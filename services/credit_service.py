"""Credit Service - fetches and caches release credits from Discogs.

Builds a weighted person graph (producers, engineers, musicians) and generates
a concise summary for inclusion in Claude prompts. Exploits Discogs's unique
credits database for recommendation signals no streaming platform uses.
"""

import json
import logging
import os
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 30
MAX_RELEASES_TO_FETCH = 20
MAX_PEOPLE_IN_SUMMARY = 18
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
DISCOGS_RATE_DELAY = 1.1  # seconds between requests

# Role weights: higher = stronger signal of musical influence
ROLE_WEIGHTS = {
    # Creative roles (highest signal)
    "Producer": 5.0,
    "Co-producer": 4.0,
    "Written-By": 4.5,
    "Composed By": 4.5,
    "Songwriter": 4.5,
    "Music By": 4.0,
    "Lyrics By": 3.0,
    "Arranged By": 3.5,
    "Remix": 4.0,
    # Performance roles
    "Vocals": 3.5,
    "Bass": 3.0,
    "Guitar": 3.0,
    "Drums": 3.0,
    "Keyboards": 3.0,
    "Synthesizer": 3.0,
    "Piano": 3.0,
    "Percussion": 2.5,
    "Saxophone": 3.0,
    "Trumpet": 3.0,
    "Strings": 2.5,
    "Featuring": 3.0,
    # Technical roles
    "Mixed By": 2.5,
    "Engineer": 2.0,
    "Recorded By": 2.0,
    "Programmed By": 2.5,
    "Executive Producer": 2.0,
    # Mastering
    "Mastered By": 1.0,
    "Mastering": 1.0,
    "Lacquer Cut By": 0.5,
    # Non-musical (filtered out)
    "Pressed By": 0.0,
    "Design": 0.0,
    "Photography By": 0.0,
    "Artwork By": 0.0,
    "Artwork": 0.0,
    "Cover": 0.0,
    "Liner Notes": 0.0,
    "Management": 0.0,
}
DEFAULT_ROLE_WEIGHT = 1.0
MINIMUM_ROLE_WEIGHT = 0.5

# Role normalization map
_ROLE_NORMALIZE = {
    "written-by": "Written-By",
    "written by": "Written-By",
    "composed by": "Composed By",
    "produced by": "Producer",
    "co-producer": "Co-producer",
    "mixed by": "Mixed By",
    "recorded by": "Recorded By",
    "mastered by": "Mastered By",
    "lacquer cut by": "Lacquer Cut By",
    "bass guitar": "Bass",
    "electric bass": "Bass",
    "lead guitar": "Guitar",
    "rhythm guitar": "Guitar",
    "electric guitar": "Guitar",
    "acoustic guitar": "Guitar",
    "lead vocals": "Vocals",
    "backing vocals": "Vocals",
    "programmed by": "Programmed By",
    "arranged by": "Arranged By",
    "remix": "Remix",
    "remixed by": "Remix",
    "executive producer": "Executive Producer",
    "executive-producer": "Executive Producer",
    "featuring": "Featuring",
    "feat.": "Featuring",
}

# Role display groupings for the prompt
ROLE_GROUPS = [
    ("Producers & Writers",
     ["Producer", "Co-producer", "Written-By", "Composed By",
      "Songwriter", "Music By", "Arranged By"]),
    ("Musicians",
     ["Bass", "Guitar", "Drums", "Keyboards", "Vocals", "Synthesizer",
      "Percussion", "Piano", "Saxophone", "Trumpet", "Strings", "Featuring"]),
    ("Engineers & Mixers",
     ["Mixed By", "Engineer", "Recorded By", "Remix", "Programmed By"]),
    ("Mastering",
     ["Mastered By", "Mastering"]),
]


def _get_role_weight(role: str) -> float:
    """Look up role weight with fuzzy matching."""
    role_lower = role.lower().strip()
    for canonical, weight in ROLE_WEIGHTS.items():
        if canonical.lower() == role_lower:
            return weight
    for canonical, weight in ROLE_WEIGHTS.items():
        if canonical.lower() in role_lower or role_lower in canonical.lower():
            return weight
    return DEFAULT_ROLE_WEIGHT


def _normalize_role(raw_role: str) -> str:
    """Normalize a Discogs role string to a canonical display form."""
    role = raw_role.strip()
    normalized = _ROLE_NORMALIZE.get(role.lower())
    if normalized:
        return normalized
    return role


@dataclass
class PersonNode:
    """A person appearing across the user's collection."""
    person_id: int
    name: str
    role_releases: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list))

    @property
    def total_releases(self) -> int:
        all_releases = set()
        for releases in self.role_releases.values():
            all_releases.update(releases)
        return len(all_releases)

    @property
    def weighted_score(self) -> float:
        score = 0.0
        for role, releases in self.role_releases.items():
            weight = _get_role_weight(role)
            score += weight * len(releases)
        return score

    @property
    def primary_role(self) -> str:
        best_role = ""
        best_score = 0.0
        for role, releases in self.role_releases.items():
            s = _get_role_weight(role) * len(releases)
            if s > best_score:
                best_score = s
                best_role = role
        return best_role


class CreditService:
    """Fetches and caches release credits from Discogs, builds a person graph,
    and generates a concise summary for the LLM prompt."""

    def __init__(self, discogs_service=None):
        self.discogs = discogs_service

    def get_credit_summary(
        self,
        collection: list[dict],
        data_dir: Path | None = None,
        max_fetch: int = MAX_RELEASES_TO_FETCH,
        on_progress=None,
    ) -> str:
        """Build a credit-based person summary for the Claude prompt.

        Args:
            collection: The user's full Discogs collection.
            data_dir: Per-user data directory.
            max_fetch: Maximum uncached releases to fetch from API.
            on_progress: Optional callback(message, percent) for SSE updates.

        Returns:
            A multi-line string for the LLM prompt, or "" if unavailable.
        """
        if not self.discogs:
            return ""

        d = data_dir or Path("data")
        cache_data = self._load_cache(d)

        ranked = self._rank_releases(collection)
        to_fetch = []
        for release in ranked:
            rid = str(release.get("id", ""))
            if not rid:
                continue
            cached_entry = cache_data.get(rid)
            if cached_entry and not self._is_expired(cached_entry):
                continue
            to_fetch.append(release)
            if len(to_fetch) >= max_fetch:
                break

        if to_fetch:
            if on_progress:
                on_progress(
                    f"Fetching credits for {len(to_fetch)} releases...", 12)
            for i, release in enumerate(to_fetch):
                try:
                    credits = self._fetch_release_credits(release["id"])
                    cache_data[str(release["id"])] = {
                        "release_title": release.get("title", ""),
                        "release_artists": release.get("artists", []),
                        "credits": credits,
                        "fetched_at": datetime.now().isoformat(),
                    }
                except Exception as e:
                    logger.warning(
                        "Failed to fetch credits for release %s: %s",
                        release.get("id"), e)
                if i < len(to_fetch) - 1:
                    time.sleep(DISCOGS_RATE_DELAY)

            self._save_cache(d, cache_data)

        collection_ids = {str(r.get("id", "")) for r in collection}
        graph = self._build_graph(cache_data, collection_ids)
        return self._format_summary(graph)

    def _rank_releases(self, collection: list[dict]) -> list[dict]:
        """Rank releases by representativeness (genre/style overlap)."""
        genre_counts: Counter = Counter()
        style_counts: Counter = Counter()
        for r in collection:
            for g in r.get("genres", []):
                genre_counts[g] += 1
            for s in r.get("styles", []):
                style_counts[s] += 1

        def _score(release: dict) -> float:
            score = 0.0
            for g in release.get("genres", []):
                score += genre_counts.get(g, 0)
            for s in release.get("styles", []):
                score += style_counts.get(s, 0) * 1.5
            return score

        return sorted(collection, key=_score, reverse=True)

    def _fetch_release_credits(self, release_id: int) -> list[dict]:
        """Fetch extraartists from a single Discogs release."""
        release = self.discogs._rate_limited_call(
            self.discogs.client.release, release_id
        )
        credits = []
        try:
            extra = getattr(release, 'credits', None)
            if extra is None:
                extra = getattr(release, 'extraartists', [])
            if extra is None:
                extra = []
            for artist in extra:
                raw_role = getattr(artist, "role", "") or ""
                if not raw_role:
                    continue
                roles = [_normalize_role(r.strip())
                         for r in raw_role.split(",") if r.strip()]
                roles = [r for r in roles
                         if _get_role_weight(r) >= MINIMUM_ROLE_WEIGHT]
                if not roles:
                    continue
                credits.append({
                    "person_id": getattr(artist, "id", 0),
                    "name": getattr(artist, "name", "Unknown"),
                    "roles": roles,
                })
        except Exception as e:
            logger.warning("Error parsing credits for release %s: %s",
                           release_id, e)
        return credits

    def _build_graph(self, cache_data: dict,
                     collection_ids: set[str]) -> dict[int, PersonNode]:
        """Build the person graph from cached credit data."""
        graph: dict[int, PersonNode] = {}

        for rid, entry in cache_data.items():
            if rid not in collection_ids:
                continue
            release_title = entry.get("release_title", f"Release {rid}")
            artists_str = ", ".join(entry.get("release_artists", []))
            display = (f"{artists_str} - {release_title}"
                       if artists_str else release_title)

            for credit in entry.get("credits", []):
                pid = credit.get("person_id")
                if not pid:
                    continue
                if pid not in graph:
                    graph[pid] = PersonNode(
                        person_id=pid,
                        name=credit.get("name", "Unknown"),
                    )
                node = graph[pid]
                for role in credit.get("roles", []):
                    node.role_releases[role].append(display)

        return graph

    def _format_summary(self, graph: dict[int, PersonNode],
                        max_people: int = MAX_PEOPLE_IN_SUMMARY) -> str:
        """Format the person graph into a concise prompt block."""
        significant = {pid: node for pid, node in graph.items()
                       if node.total_releases >= 2}

        if not significant:
            return ""

        ranked = sorted(significant.values(),
                        key=lambda n: n.weighted_score, reverse=True)
        top = ranked[:max_people]

        lines = [
            "KEY PEOPLE across your collection "
            "(producers, musicians, engineers):"
        ]

        for group_name, group_roles in ROLE_GROUPS:
            group_people = []
            for node in top:
                matching_roles = [r for r in node.role_releases
                                  if r in group_roles]
                if matching_roles:
                    group_people.append((node, matching_roles))

            if not group_people:
                continue

            lines.append(f"  {group_name}:")
            for node, roles in group_people:
                role_parts = []
                for role in roles:
                    releases = node.role_releases[role]
                    if len(releases) <= 3:
                        release_list = ", ".join(releases)
                    else:
                        release_list = (
                            ", ".join(releases[:2])
                            + f" +{len(releases)-2} more"
                        )
                    role_parts.append(f"{role} on {release_list}")
                lines.append(
                    f"    - {node.name}: {'; '.join(role_parts)}")

        return "\n".join(lines) if len(lines) > 1 else ""

    def _load_cache(self, data_dir: Path) -> dict:
        cache_file = data_dir / "credits_cache.json"
        if not cache_file.exists():
            return {}
        try:
            raw = cache_file.read_text(encoding="utf-8")
            if len(raw) > MAX_FILE_SIZE:
                logger.warning("Credits cache exceeds size limit; "
                               "returning empty")
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError, OSError):
            return {}

    def _save_cache(self, data_dir: Path, cache_data: dict) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_file = data_dir / "credits_cache.json"
        _atomic_write_json(cache_file, cache_data)

    @staticmethod
    def _is_expired(entry: dict) -> bool:
        try:
            fetched = datetime.fromisoformat(
                entry.get("fetched_at", ""))
            return datetime.now() - fetched > timedelta(days=CACHE_TTL_DAYS)
        except (ValueError, TypeError):
            return True


def _atomic_write_json(filepath: Path, data) -> None:
    """Write JSON atomically using temp file + rename."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(filepath.parent), suffix=".tmp", prefix=".credits_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=True)
        if filepath.exists():
            filepath.unlink()
        os.rename(tmp_path, str(filepath))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
