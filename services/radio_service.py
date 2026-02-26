import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import httpx
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

    @staticmethod
    def _spread_artists(songs: list[dict]) -> list[dict]:
        """Reorder songs so no two consecutive tracks share the same artist.

        Uses a greedy approach: pick the next song whose artist differs from the
        previous one.  Falls back to allowing a repeat only when no other option
        remains (e.g. a single artist dominates the list).
        """
        if len(songs) <= 1:
            return songs

        remaining = list(songs)
        result: list[dict] = [remaining.pop(0)]

        while remaining:
            last_artist = result[-1].get("artist", "").lower().strip()
            # Find first candidate whose artist differs
            for i, song in enumerate(remaining):
                if song.get("artist", "").lower().strip() != last_artist:
                    result.append(remaining.pop(i))
                    break
            else:
                # Every remaining song is the same artist — just take one
                result.append(remaining.pop(0))

        return result

    def _batched_generate(self, build_prompts, num_songs: int,
                          ai_model: str = "claude-sonnet",
                          on_batch=None,
                          exclude_tracks: list[dict] | None = None,
                          exclude_set: set[tuple[str, str]] | None = None,
                          era_from: int | None = None,
                          era_to: int | None = None) -> list[dict]:
        """Generate songs using parallel LLM calls, then dedup and era-filter.

        Fires multiple requests simultaneously for speed, each with a
        different variety hint to encourage diversity, then deduplicates.
        exclude_tracks: optional list of source tracks to filter out (e.g. from Spotify input).
        exclude_set: set of (artist_lower, title_lower) tuples to hard-filter (rec history, dislikes).
        era_from/era_to: if set, post-filter songs whose year falls outside the range.
        """
        has_era = bool(era_from or era_to)
        is_ollama = ai_model == "ollama"
        is_haiku = ai_model == "claude-haiku"
        if is_ollama:
            effective_batch = self.OLLAMA_BATCH_SIZE
        elif is_haiku:
            effective_batch = self.HAIKU_BATCH_SIZE
        else:
            effective_batch = self.BATCH_SIZE

        # Over-request to compensate for era filtering and exclude_set hard-filtering
        over_request = 0
        if has_era and (is_ollama or is_haiku):
            over_request += int(num_songs * 0.4)
        if exclude_set and len(exclude_set) > 50:
            # LLMs often regenerate the same popular songs — request extra to compensate
            over_request += int(num_songs * 0.3)
        target = num_songs + over_request

        num_workers = max(1, (target + effective_batch - 1) // effective_batch)
        # Cap workers: Ollama 3 (local GPU), Haiku 3 (cheap+fast), Sonnet 2 (API cost)
        max_workers = 3 if (is_ollama or is_haiku) else 2
        num_workers = min(max_workers, num_workers)

        def _max_tokens_for(batch_size):
            if is_ollama:
                return max(3000, batch_size * 150)
            elif is_haiku:
                return max(4000, batch_size * 150)
            return max(4000, batch_size * 150)

        def _run_one(idx):
            batch_size = min(effective_batch, target)
            hint = self._VARIETY_HINTS[idx % len(self._VARIETY_HINTS)]
            system_text, user_text = build_prompts(batch_size, hint)
            return self._call_and_parse(system_text, user_text,
                                        ai_model=ai_model,
                                        max_tokens=_max_tokens_for(batch_size))

        logger.info("Parallel %s: %d workers x %d songs each (era_filter=%s, over_request=%d)",
                    ai_model, num_workers, min(effective_batch, target), has_era, over_request)

        all_songs: list[dict] = []
        seen: set[tuple[str, str]] = set()
        total_era_rejected = 0

        # Pre-seed seen set with source tracks so generated songs don't duplicate them
        if exclude_tracks:
            for t in exclude_tracks:
                seen.add((t.get("artist", "").lower().strip(),
                          t.get("title", "").lower().strip()))

        # Hard-filter: previously recommended songs + disliked songs
        if exclude_set:
            seen.update(exclude_set)
            logger.info("Hard-filter: pre-seeded %d songs from rec history + dislikes", len(exclude_set))

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

                # Dedup first
                deduped = []
                for song in batch:
                    key = (song.get("artist", "").lower().strip(),
                           song.get("title", "").lower().strip())
                    if key not in seen:
                        seen.add(key)
                        deduped.append(song)

                # Era filter
                if has_era:
                    kept, rejected = self._filter_by_era(deduped, era_from, era_to)
                    if rejected:
                        total_era_rejected += len(rejected)
                        logger.info("Era filter: rejected %d songs from batch %d: %s",
                                    len(rejected), i,
                                    [(s.get("artist", "?"), s.get("title", "?"), s.get("year", "?"))
                                     for s in rejected[:5]])
                    all_songs.extend(kept)
                else:
                    all_songs.extend(deduped)

                logger.info("Parallel batch %d: +%d songs, total valid %d/%d",
                            i, len(deduped), len(all_songs), num_songs)
                if on_batch:
                    on_batch(min(len(all_songs), num_songs), num_songs)

        # If parallel didn't reach target (especially after era filtering), do a top-up
        if len(all_songs) < num_songs:
            remaining = num_songs - len(all_songs)
            # Request extra to compensate for expected era rejections
            request_count = remaining + (int(remaining * 0.5) if has_era else 0)
            lines = [f"  - {s.get('artist','')} - {s.get('title','')}" for s in all_songs]
            already_picked = "\n\nALREADY SELECTED (do NOT repeat):\n" + "\n".join(lines)
            system_text, user_text = build_prompts(request_count, already_picked)
            batch = self._call_and_parse(system_text, user_text,
                                         ai_model=ai_model,
                                         max_tokens=_max_tokens_for(request_count))
            if batch:
                deduped = []
                for song in batch:
                    key = (song.get("artist", "").lower().strip(),
                           song.get("title", "").lower().strip())
                    if key not in seen:
                        seen.add(key)
                        deduped.append(song)

                if has_era:
                    kept, rejected = self._filter_by_era(deduped, era_from, era_to)
                    if rejected:
                        total_era_rejected += len(rejected)
                        logger.info("Era filter (top-up): rejected %d more songs", len(rejected))
                    all_songs.extend(kept)
                else:
                    all_songs.extend(deduped)
                logger.info("Top-up: total valid %d/%d", len(all_songs), num_songs)

        if total_era_rejected:
            logger.warning("Era filter total: rejected %d/%d songs outside %s-%s range",
                           total_era_rejected, len(all_songs) + total_era_rejected,
                           era_from or "?", era_to or "?")

        return self._spread_artists(all_songs[:num_songs])

    # Named discovery tiers for meaningful slider labels
    DISCOVERY_TIERS = [
        {"max": 15, "name": "Comfort Zone", "label": "Deep cuts from artists you already love"},
        {"max": 30, "name": "Familiar Ground", "label": "Same scenes, labels, and close collaborators"},
        {"max": 50, "name": "Near Orbit", "label": "Adjacent genres, shared producers, related movements"},
        {"max": 70, "name": "Explorer", "label": "Cross-genre connections, unexpected bridges"},
        {"max": 85, "name": "Adventurer", "label": "Different eras, countries, and sonic territories"},
        {"max": 100, "name": "Deep Space", "label": "Wildcard picks with only a thin thread back to your taste"},
    ]

    def _discovery_guidance(self, discovery: int) -> str:
        """Return prompt guidance based on the discovery slider level (0-100)."""
        if discovery <= 15:
            return (
                "DISCOVERY TIER: Comfort Zone (deep cuts from owned artists)\n"
                "- 90%+ songs should be by artists IN the listener's collection or their direct side-projects/collaborators.\n"
                "- Prioritize: album deep cuts, B-sides, rare singles, remixes, and live versions by collection artists.\n"
                "- The remaining picks can be label-mates or same-session musicians.\n"
                "- The listener wants to rediscover what they already own — surprise them WITHIN their collection."
            )
        elif discovery <= 30:
            return (
                "DISCOVERY TIER: Familiar Ground (same scenes and collaborators)\n"
                "- 70% should be artists closely connected to the collection: same labels, producers, session musicians, scene peers.\n"
                "- 30% can be adjacent artists the listener likely knows but doesn't own yet.\n"
                "- Stay within the same broad genres and eras as the collection.\n"
                "- Focus on: label-mate albums, producer discographies, scene compilations."
            )
        elif discovery <= 50:
            return (
                "DISCOVERY TIER: Near Orbit (adjacent genres and movements)\n"
                "- 50% familiar territory — same genres, related scenes.\n"
                "- 50% adjacent discoveries — neighboring genres, parallel movements in other countries, genre precursors/descendants.\n"
                "- Connect through specific musical attributes: shared instrumentation, similar production techniques, tempo/mood overlap.\n"
                "- Example: if they own post-punk, introduce no-wave, coldwave, or early industrial."
            )
        elif discovery <= 70:
            return (
                "DISCOVERY TIER: Explorer (cross-genre bridges)\n"
                "- 30% familiar anchor points from their genres.\n"
                "- 70% new territory connected through deeper musical DNA.\n"
                "- Cross genre boundaries freely: connect through shared textures, rhythmic patterns, harmonic language, or emotional tone.\n"
                "- Bridge eras: a 1968 Brazilian tropicalia track can connect to a 2020 indie folk song through similar guitar voicings.\n"
                "- Prioritize: artists from different countries, overlooked genre-bridging albums."
            )
        elif discovery <= 85:
            return (
                "DISCOVERY TIER: Adventurer (different sonic territories)\n"
                "- 15% familiar anchors only.\n"
                "- 85% genuinely unfamiliar territory.\n"
                "- Connect through abstract musical properties: rhythmic complexity, harmonic tension, dynamic range, vocal texture.\n"
                "- International deep cuts, overlooked micro-genres, compilation-only tracks.\n"
                "- The connection to their taste should be real but non-obvious — explain it clearly."
            )
        else:
            return (
                "DISCOVERY TIER: Deep Space (wildcard exploration)\n"
                "- 95% should be artists the listener has almost certainly NEVER heard of.\n"
                "- Only the thinnest thread connects back to their taste — a shared mood, a production technique, an abstract sonic quality.\n"
                "- Pull from: field recordings, global folk traditions, avant-garde, micro-scenes, unreleased/limited-press records.\n"
                "- The goal is genuine musical education — 'I had no idea this existed.'\n"
                "- Still explain the connection, even if it's abstract."
            )

    def _era_guidance(self, era_from: int | None, era_to: int | None) -> str:
        """Return prompt block constraining songs to a year range."""
        if not era_from and not era_to:
            return ""
        if era_from and era_to:
            return (
                f"\n*** ERA CONSTRAINT (MANDATORY): ONLY recommend songs released between "
                f"{era_from} and {era_to}. Every song MUST have a \"year\" value within "
                f"{era_from}-{era_to}. Songs outside this range will be REJECTED. ***\n"
            )
        if era_from:
            return (
                f"\n*** ERA CONSTRAINT (MANDATORY): ONLY recommend songs released from "
                f"{era_from} onward. Every song MUST have a \"year\" value of {era_from} or "
                f"later. Songs before {era_from} will be REJECTED. ***\n"
            )
        return (
            f"\n*** ERA CONSTRAINT (MANDATORY): ONLY recommend songs released up to "
            f"{era_to}. Every song MUST have a \"year\" value of {era_to} or earlier. "
            f"Songs after {era_to} will be REJECTED. ***\n"
        )

    @staticmethod
    def _filter_by_era(songs: list[dict], era_from: int | None,
                       era_to: int | None) -> tuple[list[dict], list[dict]]:
        """Filter songs that violate era constraints. Returns (kept, rejected)."""
        if not era_from and not era_to:
            return songs, []

        kept, rejected = [], []
        for song in songs:
            year_raw = song.get("year")
            # Parse year from various formats: int, "1985", "1985-01-01", etc.
            year = None
            if isinstance(year_raw, int) and 1900 <= year_raw <= 2099:
                year = year_raw
            elif isinstance(year_raw, str):
                # Extract first 4-digit year from string
                m = re.search(r'\b(19\d{2}|20\d{2})\b', year_raw)
                if m:
                    year = int(m.group(1))

            if year is None:
                # No parseable year — reject when era is constrained
                rejected.append(song)
                continue

            # Normalize the parsed year back onto the song for downstream use
            song["year"] = year

            if era_from and year < era_from:
                rejected.append(song)
            elif era_to and year > era_to:
                rejected.append(song)
            else:
                kept.append(song)

        return kept, rejected

    def generate_playlist(self, profile: dict, collection: list[dict],
                          thumbs_summary: str = "",
                          dislikes_summary: str = "",
                          play_history_summary: str = "",
                          exclude_set: set[tuple[str, str]] | None = None,
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
{era_guide}IMPORTANT: Maximize variety — never include more than 1 song per artist. Spread across different artists, genres, and decades.
Only recommend songs that ACTUALLY EXIST — real artists, real titles, real albums.
The "year" field MUST be the correct, actual release year of each song.
Return a JSON array of objects with keys: artist, title, album, year, reason, match_score, match_attributes, similar_to
- "reason": 1 specific sentence about WHY this song connects (name the musical attribute, not just "similar vibes")
- "match_score": integer 1-100 confidence rating (90+=near-perfect, 70-89=strong, 50-69=moderate, 30-49=adventurous, 1-29=wildcard)
- "match_attributes": array of 1-3 tags like "shared producer", "same label", "similar instrumentation", "production style", "mood/atmosphere", "genre lineage", "tempo/energy", "vocal texture"
- "similar_to": array of 1-2 artist names from the listener's collection that connect to this pick
Example: "similar_to": ["Radiohead", "Massive Attack"]
Return ONLY the JSON array, no other text."""

                user_text = f"""Recommend {batch_size} songs for a listener with this taste:
{era_guide}{summary}
{f"Liked: {thumbs_summary}" if thumbs_summary else ""}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
{f"Recently played (DO NOT repeat): {play_history_summary}" if play_history_summary else ""}
Discovery: {discovery}/100 (0=familiar, 100=adventurous)
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once.
{discovery_guide}
{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge — deeper than
Spotify, Last.fm, or Pandora. Your recommendations should surprise and delight,
not just serve safe, obvious picks. Go beyond surface-level genre matching.

CURATION PHILOSOPHY:
- Map musical DNA precisely: shared producers, session musicians, engineers, label mates,
  scene connections, sample sources — not just "similar sounding" acts
- Identify SPECIFIC sonic attributes that connect tracks: instrumentation, production techniques,
  tempo range, harmonic language, rhythmic patterns, vocal texture, dynamic range
- Create flow: sequence songs so each transition feels intentional (tempo, mood, key)
- Pull from any era or country — a 1972 Japanese psych track can follow a 2023 post-punk single
- If they have thumbs-up history, lean INTO those preferences but still push boundaries
- NEVER repeat a song from the thumbs-up history or the disliked list
- Avoid overly obvious hits — dig for the deeper cuts
- VARIETY IS CRITICAL: never include more than 1 song per artist. Every song must be by a DIFFERENT artist.
  Spread picks across different genres, decades, labels, and countries.

ACCURACY RULES:
- Only recommend songs that ACTUALLY EXIST. Do not invent fake tracks or albums.
- Verify artist names, song titles, and album names are real and correct.
- If unsure about a specific track, pick a different one you're confident about.
- The "year" must be the actual release year of that specific recording.

For EACH song you MUST include:

1. "reason": A specific 1-2 sentence explanation of WHY this song connects to the listener's taste.
   Be precise — name the specific musical attributes (not just "similar vibes").
   Good: "Shares the same Conny Plank production style and motorik drumming as your Neu! records"
   Bad: "Similar experimental rock feel"

2. "match_score": An integer from 1-100 representing how confident you are this is a good match.
   90-100: Near-perfect match — shares multiple concrete connections (producer, musicians, label, scene, sound)
   70-89: Strong match — clear sonic/stylistic connection with identifiable shared attributes
   50-69: Moderate match — connected through genre or broad style, fewer specific links
   30-49: Adventurous pick — connected through abstract qualities (mood, texture, energy)
   1-29: Wildcard — tenuous connection, meant to expand horizons

3. "match_attributes": An array of 1-4 specific musical dimensions that connect this song to the listener's taste.
   Each attribute is a short tag from this vocabulary:
   "shared producer", "same label", "scene peers", "shared musicians", "similar instrumentation",
   "production style", "vocal texture", "rhythmic pattern", "harmonic language", "tempo/energy",
   "mood/atmosphere", "genre lineage", "sample source", "influenced by", "same movement",
   "geographic scene", "era peers", "sonic palette"

4. "similar_to": An array of 1-3 specific artist+album combos FROM THE LISTENER'S COLLECTION
   that this song connects to, with a specific explanation of the musical connection.

Return a JSON array of exactly {batch_size} objects with these keys:
"artist", "title", "album", "year", "reason", "match_score", "match_attributes", "similar_to"

The "similar_to" should be an array like: [{{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich, similar layered guitar textures and electronic underpinnings"}}]

Return ONLY the JSON array, no other text."""

                user_text = f"""Create a radio playlist of {batch_size} SONGS based on this listener's Discogs collection.
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once in your list.

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
                                      on_batch=on_batch,
                                      exclude_set=exclude_set,
                                      era_from=era_from, era_to=era_to)

    def resolve_youtube_ids(self, playlist: list[dict]) -> list[dict]:
        """Find YouTube video IDs, correct metadata from YT title, and fetch album info."""
        def _resolve_one(song):
            artist = song.get("artist", "")
            title = song.get("title", "")
            video_info = self._find_youtube_video(artist, title)
            if video_info:
                song["videoId"] = video_info["videoId"]
                song["thumbnail"] = video_info["thumbnail"]
                song["duration"] = video_info["duration"]
                song["altVideoIds"] = video_info.get("altVideoIds", [])
                song["ytTitle"] = video_info.get("ytTitle", "")

                # Parse YouTube title as source of truth for artist/song name
                yt_artist, yt_song = self._parse_youtube_title(
                    video_info.get("ytTitle", ""),
                    video_info.get("ytChannel", ""),
                )
                if yt_artist and yt_song:
                    song["artist"] = yt_artist
                    song["title"] = yt_song
                elif yt_song:
                    # Got a song name but no artist — keep LLM artist
                    song["title"] = yt_song

                return song
            return None

        def _enrich_metadata(song):
            """Look up album name, year, and artwork via iTunes/Deezer."""
            artist = song.get("artist", "")
            title = song.get("title", "")
            meta = self._fetch_song_metadata(artist, title)
            song["albumArt"] = meta.get("albumArt", "")
            if meta.get("album"):
                song["album"] = meta["album"]
            if meta.get("year"):
                song["year"] = meta["year"]

        resolved = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            # Resolve YouTube IDs + correct metadata from YT titles
            yt_futures = {pool.submit(_resolve_one, song): i for i, song in enumerate(playlist)}
            results = [None] * len(playlist)
            for future in yt_futures:
                idx = yt_futures[future]
                results[idx] = future.result()
            resolved = [r for r in results if r is not None]

            # Enrich with album name/year/art using corrected artist+title
            meta_futures = [pool.submit(_enrich_metadata, song) for song in resolved]
            for f in meta_futures:
                f.result()

        return resolved

    # Patterns that indicate a live performance — skip these YouTube results
    _LIVE_PATTERNS = re.compile(
        r'\b(?:live\s+at|live\s+from|live\s+on|live\s+in|live\s+session'
        r'|bbc\s+session|peel\s+session|concert|live\))',
        re.IGNORECASE,
    )

    # Patterns stripped from YouTube titles before parsing artist/song
    _YT_SUFFIX_PATTERNS = re.compile(
        r'\s*[\(\[](official\s*(music\s*)?video|official\s*audio|official\s*lyric'
        r'|lyric\s*video|lyrics?|audio|visuali[sz]er|music\s*video|mv|hd|hq'
        r'|remaster(ed)?|full\s*album|video\s*oficial|clip\s*officiel'
        r'|feat\.?[^)\]]*|ft\.?[^)\]]*)[\)\]]',
        re.IGNORECASE,
    )

    @staticmethod
    def _parse_youtube_title(yt_title: str, channel_name: str = "") -> tuple[str, str]:
        """Extract (artist, song_title) from a YouTube video title.

        YouTube titles commonly follow 'Artist - Song Title (Official Audio)'.
        Falls back to channel name as artist if no separator found.
        """
        if not yt_title:
            return ("", "")

        # Strip common suffixes like (Official Audio), [Music Video], etc.
        cleaned = RadioService._YT_SUFFIX_PATTERNS.sub("", yt_title).strip()
        # Also strip trailing whitespace and pipes/slashes sometimes used
        cleaned = re.sub(r'\s*[|/]\s*$', '', cleaned).strip()

        # Try splitting on ' - ' (most common YouTube format)
        if ' - ' in cleaned:
            parts = cleaned.split(' - ', 1)
            artist = parts[0].strip()
            song = parts[1].strip()
            if artist and song:
                return (artist, song)

        # Try splitting on ' – ' (en-dash, also common)
        if ' \u2013 ' in cleaned:
            parts = cleaned.split(' \u2013 ', 1)
            artist = parts[0].strip()
            song = parts[1].strip()
            if artist and song:
                return (artist, song)

        # No separator — use channel name as artist, full title as song
        ch = channel_name.strip()
        # Strip " - Topic" suffix from YouTube auto-generated channels
        ch = re.sub(r'\s*-\s*Topic$', '', ch, flags=re.IGNORECASE).strip()
        if ch:
            return (ch, cleaned if cleaned else yt_title.strip())

        return ("", cleaned if cleaned else yt_title.strip())

    def _find_youtube_video(self, artist: str, title: str) -> dict | None:
        """Search YouTube for a song, return video info with backup IDs. Cached 24hr."""
        cache_key = f"yt:{artist.lower()}:{title.lower()}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        all_results = []
        try:
            search = VideosSearch(f"{artist} {title} official audio", limit=8)
            all_results = search.result().get("result", [])
            if not all_results:
                search = VideosSearch(f"{artist} {title}", limit=8)
                all_results = search.result().get("result", [])
        except Exception:
            pass

        if not all_results:
            return None

        def _duration_seconds(d: str) -> int:
            """Parse '3:45' or '1:02:30' to seconds. Returns -1 if unparseable."""
            if not d:
                return -1
            try:
                parts = d.split(":")
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
            except (ValueError, AttributeError):
                pass
            return -1

        MAX_DURATION = 420  # 7 minutes — filter out long videos / full albums

        def _is_acceptable(r) -> bool:
            """Return True if the result isn't too long and isn't a live recording."""
            dur = _duration_seconds(r.get("duration", ""))
            if dur <= 0 or dur > MAX_DURATION:
                return False
            yt_title = r.get("title", "")
            if self._LIVE_PATTERNS.search(yt_title):
                return False
            return True

        def _title_match_score(r) -> tuple[int, int, int]:
            """Score how well a YouTube result title matches the expected song.

            Returns (total_score, title_score, artist_score) so callers can
            enforce independent minimums on both components.

            Scoring (max 100):
              - Title word match: up to 40 points
              - Artist word match: up to 40 points
              - "official" bonus: 10 points
              - Channel name matches artist: 10 points
            """
            yt_raw = r.get("title", "").lower()
            yt_norm = re.sub(r"[^a-z0-9\s]", "", yt_raw)
            artist_l = re.sub(r"[^a-z0-9\s]", "", artist.lower())
            title_l = re.sub(r"[^a-z0-9\s]", "", title.lower())
            title_score = 0
            artist_score = 0
            bonus = 0
            # Title words present in YT title
            title_words = [w for w in title_l.split() if len(w) > 1]
            if title_words:
                matched = sum(1 for w in title_words if w in yt_norm)
                title_score = int(40 * matched / len(title_words))
            # Artist words present in YT title
            artist_words = [w for w in artist_l.split() if len(w) > 1]
            if artist_words:
                matched = sum(1 for w in artist_words if w in yt_norm)
                artist_score = int(40 * matched / len(artist_words))
            # Check channel name for artist match (often "ArtistName - Topic")
            channel = re.sub(r"[^a-z0-9\s]", "",
                             r.get("channel", {}).get("name", "").lower()
                             if isinstance(r.get("channel"), dict)
                             else "")
            if channel and artist_words:
                ch_matched = sum(1 for w in artist_words if w in channel)
                bonus += int(10 * ch_matched / len(artist_words))
            # Small bonus for "official" in title
            if "official" in yt_norm:
                bonus += 10
            return (title_score + artist_score + bonus, title_score, artist_score)

        filtered = [r for r in all_results if _is_acceptable(r)]
        # Fall back: try duration-only filter (allow live if nothing else)
        if not filtered:
            filtered = [r for r in all_results
                         if 0 < _duration_seconds(r.get("duration", "")) <= MAX_DURATION]
        # If everything is too long / unparseable, skip this song entirely
        if not filtered:
            logger.info("YouTube: all results for '%s - %s' exceeded %ds or unparseable — skipping",
                        artist, title, MAX_DURATION)
            return None

        # Sort by total match score (best match first) instead of YouTube's rank
        filtered.sort(key=lambda r: _title_match_score(r)[0], reverse=True)
        best = filtered[0]
        best_total, best_title, best_artist = _title_match_score(best)

        # Rejection rules — must have reasonable match on BOTH title and artist
        # independently.  This prevents same-artist-wrong-song from slipping through.
        title_words_count = len([w for w in re.sub(r"[^a-z0-9\s]", "", title.lower()).split() if len(w) > 1])
        reject = False
        reject_reason = ""
        if best_total < 20:
            reject, reject_reason = True, f"total score too low ({best_total})"
        elif best_artist == 0 and best_total < 50:
            reject, reject_reason = True, f"artist absent, total only {best_total}"
        elif best_title == 0 and title_words_count > 0:
            # Artist matches but NONE of the title words found → wrong song
            reject, reject_reason = True, f"zero title word match (artist_score={best_artist})"
        elif best_title < 10 and best_total < 55:
            # Very weak title match (< 25%) with mediocre total → likely wrong song
            reject, reject_reason = True, f"weak title match ({best_title}) and low total ({best_total})"

        if reject:
            logger.warning("YouTube: rejecting '%s - %s' — best result '%s' scored total=%d title=%d artist=%d reason=%s",
                           artist, title, best.get("title", ""), best_total, best_title, best_artist, reject_reason)
            return None
        if best_total < 40:
            logger.info("YouTube: weak match for '%s - %s' (score=%d, yt='%s')",
                        artist, title, best_total, best.get("title", ""))
        best_channel = (best.get("channel", {}).get("name", "")
                        if isinstance(best.get("channel"), dict) else "")
        info = {
            "videoId": best["id"],
            "thumbnail": (best.get("thumbnails", [{}])[-1].get("url", "")
                          if best.get("thumbnails") else ""),
            "duration": best.get("duration", ""),
            "ytTitle": best.get("title", ""),
            "ytChannel": best_channel,
            "altVideoIds": [r["id"] for r in filtered[1:]],
        }
        cache.set(cache_key, info, ttl=86400)
        return info

    def _fetch_song_metadata(self, artist: str, title: str) -> dict:
        """Look up song metadata from iTunes/Deezer. Returns album art, album name, year.

        Cached 24hr. Used to enrich songs with accurate metadata after YouTube
        title parsing gives us the correct artist + song name.
        """
        cache_key = f"songmeta:{artist.lower()}:{title.lower()}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        meta: dict = {"albumArt": "", "album": "", "year": "", "artist": "", "title": ""}

        # Try iTunes Search API first
        try:
            query = urllib.parse.quote(f"{artist} {title}")
            resp = httpx.get(
                f"https://itunes.apple.com/search?term={query}&entity=song&limit=3",
                timeout=5,
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    best = None
                    artist_lower = artist.lower()
                    for r in results:
                        if artist_lower in r.get("artistName", "").lower():
                            best = r
                            break
                    if not best:
                        best = results[0]
                    art_url = best.get("artworkUrl100", "")
                    if art_url:
                        meta["albumArt"] = art_url.replace("100x100bb", "600x600bb")
                    meta["album"] = best.get("collectionName", "")
                    meta["artist"] = best.get("artistName", "")
                    meta["title"] = best.get("trackName", "")
                    release = best.get("releaseDate", "")
                    if release:
                        meta["year"] = release[:4]  # "2004-01-01T..." → "2004"
        except Exception:
            pass

        # Fallback: Deezer API (if iTunes gave no art or album)
        if not meta["albumArt"] or not meta["album"]:
            try:
                query = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
                resp = httpx.get(
                    f"https://api.deezer.com/search?q={query}&limit=1",
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if data:
                        hit = data[0]
                        if not meta["albumArt"]:
                            meta["albumArt"] = hit.get("album", {}).get("cover_big", "")
                        if not meta["album"]:
                            meta["album"] = hit.get("album", {}).get("title", "")
                        if not meta["artist"]:
                            meta["artist"] = hit.get("artist", {}).get("name", "")
                        if not meta["title"]:
                            meta["title"] = hit.get("title", "")
                        if not meta["year"]:
                            rel = hit.get("album", {}).get("release_date", "")
                            if rel:
                                meta["year"] = rel[:4]
            except Exception:
                pass

        cache.set(cache_key, meta, ttl=86400)
        return meta

    def generate_playlist_from_tracks(self, tracks: list[dict],
                                      mode: str = "similar_songs",
                                      thumbs_summary: str = "",
                                      dislikes_summary: str = "",
                                      play_history_summary: str = "",
                                      exclude_set: set[tuple[str, str]] | None = None,
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
{era_guide}IMPORTANT: Maximize variety — never include more than 1 song per artist. Spread across different artists, genres, and decades.
Only recommend songs that ACTUALLY EXIST — real artists, real titles, real albums.
The "year" field MUST be the correct, actual release year of each song.
Return a JSON array of objects with keys: artist, title, album, year, reason, match_score, match_attributes, similar_to
- "reason": 1 specific sentence about the musical connection (not just "similar vibes")
- "match_score": integer 1-100 (90+=near-perfect, 70-89=strong, 50-69=moderate, 30-49=adventurous, 1-29=wildcard)
- "match_attributes": array of 1-3 tags like "shared producer", "similar instrumentation", "production style", "mood/atmosphere", "genre lineage"
- "similar_to": array of 1-2 artist names from the input playlist that connect to this pick
Do NOT repeat songs from the input playlist. Return ONLY the JSON array."""

                user_text = f"""Recommend {batch_size} songs based on this playlist:
{era_guide}{track_listing}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
Discovery: {discovery}/100
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once.
{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge.

{philosophy}
ACCURACY RULES:
- Only recommend songs that ACTUALLY EXIST. Do not invent fake tracks or albums.
- Verify artist names, song titles, and album names are real and correct.

RULES:
- Do NOT repeat any song from the input playlist.
- NEVER repeat a song from the disliked list.
- VARIETY IS CRITICAL: never include more than 1 song per artist. Every song must be by a DIFFERENT artist.
  Spread picks across different genres, decades, labels, and countries.
- For EACH song, include all required fields below.

Return a JSON array of exactly {batch_size} objects with these keys:
"artist", "title", "album", "year", "reason", "match_score", "match_attributes", "similar_to"

- "reason": 1-2 sentences explaining the SPECIFIC musical connection (name attributes, not just "similar vibes")
- "match_score": integer 1-100 confidence rating (90+=near-perfect, 70-89=strong, 50-69=moderate, 30-49=adventurous, 1-29=wildcard)
- "match_attributes": array of 1-4 tags from: "shared producer", "same label", "scene peers", "shared musicians",
  "similar instrumentation", "production style", "vocal texture", "rhythmic pattern", "harmonic language",
  "tempo/energy", "mood/atmosphere", "genre lineage", "sample source", "influenced by", "same movement",
  "geographic scene", "era peers", "sonic palette"
- "similar_to": array like [{{"artist": "Tame Impala", "album": "Currents", "why": "same dreamy psychedelic production with layered synths"}}]

Return ONLY the JSON array, no other text."""

                user_text = f"""Create a radio playlist of {batch_size} SONGS based on this Spotify playlist.
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once in your list.

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
                                      on_batch=on_batch, exclude_tracks=tracks,
                                      exclude_set=exclude_set,
                                      era_from=era_from, era_to=era_to)

    def generate_themed_playlist(self, profile: dict, collection: list[dict],
                                    theme: str,
                                    thumbs_summary: str = "",
                                    dislikes_summary: str = "",
                                    play_history_summary: str = "",
                                    exclude_set: set[tuple[str, str]] | None = None,
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
{era_guide}IMPORTANT: Maximize variety — never include more than 1 song per artist. Spread across different artists, genres, and decades.
Only recommend songs that ACTUALLY EXIST — real artists, real titles, real albums.
The "year" field MUST be the correct, actual release year of each song.
Return a JSON array of objects with keys: artist, title, album, year, reason, match_score, match_attributes, similar_to
- "reason": 1 specific sentence about the musical connection (not just "similar vibes")
- "match_score": integer 1-100 (90+=near-perfect, 70-89=strong, 50-69=moderate, 30-49=adventurous, 1-29=wildcard)
- "match_attributes": array of 1-3 tags like "shared producer", "similar instrumentation", "production style", "mood/atmosphere", "genre lineage"
- "similar_to": array of 1-2 artist names from the listener's collection
Return ONLY the JSON array, no other text."""

                user_text = f"""Recommend {batch_size} songs matching "{theme}" for a listener with this taste:
{era_guide}{summary}
{f"Liked: {thumbs_summary}" if thumbs_summary else ""}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
Discovery: {discovery}/100
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once.
{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge.

Create a themed radio playlist of {batch_size} SONGS focused on the theme: "{theme}"
Interpret the theme broadly — it could be a genre, mood, era, activity, scenario, or vibe.

ACCURACY RULES:
- Only recommend songs that ACTUALLY EXIST. Do not invent fake tracks or albums.
- Verify artist names, song titles, and album names are real and correct.

CURATION PHILOSOPHY:
- Every song should fit the theme "{theme}"
- Still connect to the listener's taste — use their collection as a taste anchor
- Dig deep: obscure B-sides, overlooked album tracks, international gems
- Create flow: sequence songs so each transition feels intentional
- NEVER repeat a song from the disliked list
- Avoid overly obvious hits — dig for the deeper cuts
- VARIETY IS CRITICAL: never include more than 1 song per artist. Every song must be by a DIFFERENT artist.
  Spread picks across different genres, decades, labels, and countries.

For EACH song, include all required fields below.

Return a JSON array of exactly {batch_size} objects with these keys:
"artist", "title", "album", "year", "reason", "match_score", "match_attributes", "similar_to"

- "reason": 1-2 sentences explaining the SPECIFIC musical connection (name attributes, not just "similar vibes")
- "match_score": integer 1-100 confidence (90+=near-perfect, 70-89=strong, 50-69=moderate, 30-49=adventurous, 1-29=wildcard)
- "match_attributes": array of 1-4 tags from: "shared producer", "same label", "scene peers", "shared musicians",
  "similar instrumentation", "production style", "vocal texture", "rhythmic pattern", "harmonic language",
  "tempo/energy", "mood/atmosphere", "genre lineage", "sample source", "influenced by", "same movement",
  "geographic scene", "era peers", "sonic palette"
- "similar_to": array like [{{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich"}}]

Return ONLY the JSON array, no other text."""

                user_text = f"""Create a themed radio playlist based on this listener's collection.
IMPORTANT: Each song MUST be by a DIFFERENT artist. No artist should appear more than once in your list.

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
                                      on_batch=on_batch,
                                      exclude_set=exclude_set,
                                      era_from=era_from, era_to=era_to)

    def generate_replacements(
        self,
        session_liked: list[dict],
        session_disliked: list[dict],
        current_queue: list[dict],
        channel_context: dict,
        collection_summary: str = "",
        num_songs: int = 8,
        ai_model: str = "claude-haiku",
    ) -> list[dict]:
        """Generate replacement songs based on in-session feedback.

        Uses a compact, single-call prompt focused on session signals rather
        than the full collection profile.  Designed for speed (~5-10s).
        """
        discovery = channel_context.get("discovery", 30)
        discovery_guide = self._discovery_guidance(discovery)
        era_guide = self._era_guidance(
            channel_context.get("era_from"), channel_context.get("era_to"))

        # Format session signals
        def _fmt_tracks(tracks_list: list[dict], label: str) -> str:
            if not tracks_list:
                return f"No {label} yet."
            lines = []
            for t in tracks_list[-10:]:
                attrs = ", ".join(t.get("match_attributes", [])[:4])
                reason = t.get("reason", "")
                line = f"  - {t.get('artist', '?')} - {t.get('title', '?')}"
                if attrs:
                    line += f" [{attrs}]"
                if reason:
                    line += f" — {reason}"
                lines.append(line)
            return "\n".join(lines)

        liked_text = _fmt_tracks(session_liked, "likes")
        disliked_text = _fmt_tracks(session_disliked, "dislikes")

        # Queue list (artist+title only, to keep prompt compact)
        queue_lines = [
            f"  - {t.get('artist', '?')} - {t.get('title', '?')}"
            for t in current_queue[:40]
        ]
        queue_text = "\n".join(queue_lines) if queue_lines else "Queue is empty."

        # Theme context if available
        theme = channel_context.get("theme", "")
        theme_block = f'\nTHEME: Playlist is themed around "{theme}". Stay on-theme.\n' if theme else ""

        collection_block = ""
        if collection_summary:
            collection_block = f"\nLISTENER TASTE PROFILE (for context):\n{collection_summary}\n"

        system_text = f"""You are a music curator adjusting a live radio session based on real-time listener feedback.
{era_guide}
The listener is hearing a playlist and has given thumbs-up/down feedback. Adjust your picks accordingly.

RULES:
- Generate exactly {num_songs} replacement songs
- LEAN INTO what they liked — more of that energy, those attributes, those connections
- AVOID anything resembling what they disliked — same artists, similar genres, production styles, moods
- Each song must be by a DIFFERENT artist (no repeats, including from the current queue)
- Only recommend songs that ACTUALLY EXIST — real artists, real titles, real albums
- Return a JSON array of objects with keys: artist, title, album, year, reason, match_score, match_attributes, similar_to
- "reason": 1 specific sentence about WHY this fits based on what they liked
- "match_score": integer 1-100
- "match_attributes": array of 1-3 tags
- "similar_to": array of 1-2 artist names the listener would recognize
Return ONLY the JSON array, no other text."""

        user_text = f"""SESSION FEEDBACK (this listening session):

LIKED (listener wants MORE like these):
{liked_text}

DISLIKED (AVOID anything similar to these):
{disliked_text}

SONGS STILL IN QUEUE (do NOT duplicate any of these):
{queue_text}
{theme_block}{collection_block}
DISCOVERY LEVEL: {discovery}/100
{discovery_guide}
{era_guide}"""

        result = self._call_and_parse(system_text, user_text,
                                       ai_model=ai_model, max_tokens=3000)
        if not result:
            return []

        # Dedup against current queue
        queue_keys = {
            (t.get("artist", "").lower().strip(), t.get("title", "").lower().strip())
            for t in current_queue
        }
        deduped = []
        seen: set[tuple[str, str]] = set()
        for song in result:
            key = (song.get("artist", "").lower().strip(),
                   song.get("title", "").lower().strip())
            if key not in queue_keys and key not in seen:
                seen.add(key)
                deduped.append(song)

        # Era filter replacements too
        era_from_r = channel_context.get("era_from")
        era_to_r = channel_context.get("era_to")
        if era_from_r or era_to_r:
            kept, rejected = self._filter_by_era(deduped, era_from_r, era_to_r)
            if rejected:
                logger.info("Era filter (replacements): rejected %d/%d songs",
                            len(rejected), len(deduped))
            deduped = kept

        return deduped[:num_songs]

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
            "Sample releases (with styles for musical context):",
        ]
        for r in collection[:30]:
            artists = ", ".join(r.get("artists", ["Unknown"]))
            year = r.get("year", "n/a")
            styles = r.get("styles", [])
            style_str = f" [{', '.join(styles)}]" if styles else ""
            label = r.get("label", "")
            label_str = f" ({label})" if label else ""
            lines.append(f"  - {artists} - {r.get('title', '')} ({year}){style_str}{label_str}")
        return "\n".join(lines)
