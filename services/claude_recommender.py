import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.llm_provider import call_llm, parse_llm_json

logger = logging.getLogger(__name__)


class ClaudeRecommender:
    def __init__(self, api_key: str = "",
                 ollama_base_url: str = "http://localhost:11434",
                 ollama_model: str = "llama3.1:8b"):
        self.api_key = api_key
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model

    def get_recommendations(self, profile: dict, collection: list[dict],
                            preferences: str = "",
                            play_history_summary: str = "",
                            rec_history_summary: str = "",
                            era_from: int | None = None,
                            era_to: int | None = None,
                            ai_model: str = "claude-sonnet") -> list[dict]:
        """Ask LLM for music recommendations based on collection analysis."""
        summary = self._build_summary(profile, collection)

        pref_line = f"\nUSER PREFERENCES: {preferences}" if preferences else ""

        era_line = ""
        if era_from and era_to:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released between {era_from} and {era_to}. Every album MUST have a year in this range.\n"
        elif era_from:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released from {era_from} onward.\n"
        elif era_to:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released up to {era_to}.\n"

        history_block = ""
        if play_history_summary:
            history_block += f"""
RECENTLY PLAYED (the listener has heard these recently — AVOID recommending these same albums/artists):
{play_history_summary}
"""
        if rec_history_summary:
            history_block += f"""
PREVIOUSLY RECOMMENDED (you already suggested these — recommend DIFFERENT albums and artists this time):
{rec_history_summary}
"""

        system_text = """You are a knowledgeable music curator and record collector.
You recommend albums that collectors would enjoy but probably do not own yet.

For each recommendation, provide:
1. Artist - Album Title
2. Year of release
3. A brief reason why this fits (1-2 sentences)
4. The genres/styles it falls under
5. 2-3 standout tracks from the album that this specific collector would enjoy most,
   based on the genres/styles/artists in their collection. For each track, briefly
   note why it would appeal to them.

Focus on:
- Albums that bridge multiple genres/styles in the collection
- Deep cuts from labels the user already collects from
- Artists adjacent to the user's favorites (collaborators, same scene, influences)
- Both well-known essentials they might have missed and hidden gems

Return your response as a JSON array with objects containing these exact keys:
"artist", "album", "year", "reason", "genres", "styles", "tracks"

The "tracks" field should be an array of objects with keys: "title", "reason"
Example: "tracks": [{"title": "Song Name", "reason": "Heavy shoegaze textures like MBV"}]

Return ONLY the JSON array, no other text."""

        user_text = f"""Recommend 10-15 albums based on this collection analysis.

COLLECTION ANALYSIS:
{summary}
{pref_line}
{era_line}
{history_block}"""

        text = call_llm(
            system_prompt=system_text,
            user_prompt=user_text,
            provider=ai_model,
            max_tokens=2500,
            anthropic_api_key=self.api_key,
            ollama_base_url=self.ollama_base_url,
            ollama_model=self.ollama_model,
        )

        return parse_llm_json(text)

    def enrich_with_discogs(self, recommendations: list[dict],
                            discogs_service) -> list[dict]:
        """Try to find each Claude recommendation on Discogs for linking (parallel)."""
        def _lookup(rec):
            query = f"{rec.get('artist', '')} {rec.get('album', '')}"
            try:
                results = discogs_service.search(query=query, type="master", per_page=1)
                if results:
                    return results[0]
                results = discogs_service.search(query=query, type="release", per_page=1)
                return results[0] if results else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_lookup, rec): rec for rec in recommendations}
            for future in as_completed(futures):
                futures[future]["discogs_match"] = future.result()

        return recommendations

    def get_recommendations_from_tracks(self, tracks: list[dict],
                                        era_from: int | None = None,
                                        era_to: int | None = None,
                                        ai_model: str = "claude-sonnet") -> list[dict]:
        """Get album recommendations based on a list of tracks (from Spotify/upload)."""
        track_listing = self._build_track_listing(tracks)

        era_line = ""
        if era_from and era_to:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released between {era_from} and {era_to}.\n"
        elif era_from:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released from {era_from} onward.\n"
        elif era_to:
            era_line = f"\nERA CONSTRAINT: ONLY recommend albums released up to {era_to}.\n"

        system_text = """You are a knowledgeable music curator and record collector.
Based on a list of tracks the listener enjoys, recommend albums they would love.

For each recommendation, provide:
1. Artist - Album Title
2. Year of release
3. A brief reason why this fits (1-2 sentences)
4. The genres/styles it falls under
5. 2-3 standout tracks from the album

Return as a JSON array with objects containing these exact keys:
"artist", "album", "year", "reason", "genres", "styles", "tracks"
The "tracks" field: array of {"title", "reason"}
Return ONLY the JSON array."""

        user_text = f"""Recommend 10-15 albums based on these tracks:

TRACKS:
{track_listing}
{era_line}"""

        text = call_llm(
            system_prompt=system_text,
            user_prompt=user_text,
            provider=ai_model,
            max_tokens=3000,
            anthropic_api_key=self.api_key,
            ollama_base_url=self.ollama_base_url,
            ollama_model=self.ollama_model,
        )

        return parse_llm_json(text)

    def _build_track_listing(self, tracks: list[dict], max_tracks: int = 80) -> str:
        """Format tracks for LLM prompt."""
        lines = []
        for t in tracks[:max_tracks]:
            year = f" ({t['year']})" if t.get("year") else ""
            lines.append(f"  - {t.get('artist', '?')} - {t.get('title', '?')} [{t.get('album', '')}]{year}")
        return "\n".join(lines)

    def _build_summary(self, profile: dict, collection: list[dict]) -> str:
        lines = [
            f"Total releases in collection: {profile['total_releases']}",
            f"Top genres: {', '.join(f'{g} ({c})' for g, c in profile['top_genres'])}",
            f"Top styles: {', '.join(f'{s} ({c})' for s, c in profile['top_styles'])}",
            f"Top artists: {', '.join(f'{a} ({c})' for a, c in profile['top_artists'])}",
            f"Top labels: {', '.join(f'{la} ({c})' for la, c in profile['top_labels'])}",
            "",
            "Sample releases from collection:",
        ]
        for r in collection[:30]:
            artists = ", ".join(r.get("artists", ["Unknown"]))
            year = r.get("year", "n/a")
            lines.append(f"  - {artists} - {r.get('title', '')} ({year})")
        return "\n".join(lines)
