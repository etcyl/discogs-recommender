"""Scene Service - clusters a user's Discogs collection into musical scenes.

A "scene" is a group of releases that share a time period, label family,
and sonic style. Also handles label genealogy (parent/sublabel relationships).
"""

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Label-to-region mapping for inferring geographic scenes.
LABEL_REGION_MAP = {
    # Germany
    "Brain": "Dusseldorf/Cologne", "Sky Records": "Dusseldorf",
    "Ohr": "Germany", "Kosmische Musik": "Germany", "Bureau B": "Hamburg",
    "Kompakt": "Cologne", "Tresor": "Berlin", "BPitch Control": "Berlin",
    "Dial": "Hamburg", "Ostgut Ton": "Berlin",
    # UK
    "Factory": "Manchester", "Rough Trade": "London", "4AD": "London",
    "Mute": "London", "Creation Records": "London", "Warp": "Sheffield",
    "Mo Wax": "London", "Ninja Tune": "London", "XL Recordings": "London",
    "Beggars Banquet": "London", "Island": "London", "Parlophone": "London",
    "Cherry Red": "London", "Postcard Records": "Glasgow",
    "Fast Product": "Edinburgh", "Hyperdub": "London",
    "Honest Jon's": "London", "Planet Mu": "London",
    "Cooking Vinyl": "London", "Domino": "London",
    "One Little Independent": "London", "Too Pure": "London",
    # US
    "SST": "Los Angeles", "Dischord": "Washington DC",
    "Touch and Go": "Chicago", "Drag City": "Chicago",
    "Merge": "Chapel Hill", "Sub Pop": "Seattle",
    "Matador": "New York", "Kill Rock Stars": "Olympia",
    "Thrill Jockey": "Chicago", "Stones Throw": "Los Angeles",
    "Def Jam": "New York", "Blue Note": "New York",
    "Impulse!": "New York", "Stax": "Memphis", "Motown": "Detroit",
    "Numero Group": "Chicago", "Kranky": "Chicago",
    "Constellation": "Montreal", "Jagjaguwar": "Bloomington",
    "Secretly Canadian": "Bloomington", "Dead Oceans": "Bloomington",
    "4Men With Beards": "San Francisco", "Epitaph": "Los Angeles",
    "Dischord": "Washington DC", "Alternative Tentacles": "San Francisco",
    "Wax Trax!": "Chicago", "TVT": "New York",
    # Jamaica
    "Studio One": "Kingston", "Trojan": "Kingston/London",
    "Greensleeves": "London/Kingston",
    # Japan
    "Alfa": "Tokyo", "CBS/Sony": "Tokyo", "Nippon Columbia": "Tokyo",
    "Yellow Magic Orchestra": "Tokyo",
    # France
    "Ed Banger": "Paris", "Versatile": "Paris", "Infiné": "Paris",
    # Brazil
    "Som Livre": "Rio de Janeiro", "Philips (Brazil)": "Rio de Janeiro",
    # Nigeria
    "Soundway": "Lagos/London",
    # Electronic labels
    "R&S Records": "Ghent", "Rephlex": "London",
    "Ghostly International": "Ann Arbor", "100% Silk": "Los Angeles",
    "Not Not Fun": "Los Angeles", "Mexican Summer": "Brooklyn",
    "Sacred Bones": "Brooklyn", "Captured Tracks": "Brooklyn",
    # Hip-hop
    "Rhymesayers": "Minneapolis", "Def Jux": "New York",
    "Rawkus": "New York", "MF DOOM": "New York",
    "Madlib Invazion": "Los Angeles",
}


class Scene:
    """A cluster of related releases forming a musical scene."""

    def __init__(self, era_range: tuple[int, int], primary_styles: list[str],
                 labels: list[str], region: str = ""):
        self.era_range = era_range
        self.primary_styles = primary_styles
        self.labels = labels
        self.region = region
        self.releases: list[dict] = []
        self.artists: set[str] = set()

    @property
    def name(self) -> str:
        region_part = f"{self.region} " if self.region else ""
        style_part = "/".join(self.primary_styles[:2])
        era_part = f"{self.era_range[0]}-{self.era_range[1]}"
        return f"{region_part}{style_part} {era_part}"

    @property
    def summary(self) -> str:
        label_part = ", ".join(self.labels[:4])
        artist_list = ", ".join(sorted(self.artists)[:6])
        extra = f" +{len(self.artists) - 6} more" if len(self.artists) > 6 else ""
        return f"{self.name} ({label_part}): {artist_list}{extra}"


class LabelFamily:
    """A label with its parent and siblings."""

    def __init__(self, name: str, label_id: int | None = None,
                 parent_name: str = "", parent_id: int | None = None,
                 siblings: list[str] | None = None,
                 sublabels: list[str] | None = None):
        self.name = name
        self.label_id = label_id
        self.parent_name = parent_name
        self.parent_id = parent_id
        self.siblings = siblings or []
        self.sublabels = sublabels or []


class SceneService:
    MIN_SCENE_SIZE = 3
    LABEL_CACHE_TTL_DAYS = 30

    def cluster_into_scenes(self, collection: list[dict]) -> list[Scene]:
        """Group collection releases into scenes.

        Algorithm:
        1. Key each release by (decade, primary_style, primary_label)
        2. Group into buckets; keep clusters with 3+ releases
        3. Second pass: unclaimed releases grouped by (decade, primary_style)
        4. Sort by release count descending
        """
        buckets: dict[tuple, list[dict]] = defaultdict(list)

        for release in collection:
            year = self._get_year(release)
            if not year:
                continue
            decade = (year // 10) * 10
            styles = release.get("styles", [])
            primary_style = (styles[0] if styles
                             else (release.get("genres", ["Unknown"])[0]))
            labels = release.get("labels", [])
            label_key = labels[0] if labels else "Independent"
            buckets[(decade, primary_style, label_key)].append(release)

        # Broader style-only buckets for fallback
        style_buckets: dict[tuple, list[dict]] = defaultdict(list)
        for release in collection:
            year = self._get_year(release)
            if not year:
                continue
            decade = (year // 10) * 10
            styles = release.get("styles", [])
            primary_style = (styles[0] if styles
                             else (release.get("genres", ["Unknown"])[0]))
            style_buckets[(decade, primary_style)].append(release)

        scenes: list[Scene] = []
        used_releases: set = set()

        # First pass: label-specific scenes
        for (decade, style, label), releases in sorted(
            buckets.items(), key=lambda x: len(x[1]), reverse=True
        ):
            if len(releases) < self.MIN_SCENE_SIZE:
                continue

            years = [self._get_year(r) for r in releases if self._get_year(r)]
            era_range = (min(years), max(years))

            all_labels = []
            for r in releases:
                all_labels.extend(r.get("labels", []))
            label_counts = Counter(all_labels)
            top_labels = [l for l, _ in label_counts.most_common(4)]

            all_styles = []
            for r in releases:
                all_styles.extend(r.get("styles", []))
            style_counts = Counter(all_styles)
            primary_styles = [s for s, _ in style_counts.most_common(3)]

            region = self._infer_region(top_labels)

            scene = Scene(era_range, primary_styles, top_labels, region)
            for r in releases:
                rid = r.get("id")
                if rid and rid not in used_releases:
                    scene.releases.append(r)
                    used_releases.add(rid)
                    for a in r.get("artists", []):
                        scene.artists.add(a)

            if len(scene.releases) >= self.MIN_SCENE_SIZE:
                scenes.append(scene)

        # Second pass: broader style-era scenes for unclaimed releases
        for (decade, style), releases in sorted(
            style_buckets.items(), key=lambda x: len(x[1]), reverse=True
        ):
            unclaimed = [r for r in releases
                         if r.get("id") not in used_releases]
            if len(unclaimed) < self.MIN_SCENE_SIZE:
                continue

            years = [self._get_year(r) for r in unclaimed
                     if self._get_year(r)]
            if not years:
                continue
            era_range = (min(years), max(years))

            all_labels = []
            for r in unclaimed:
                all_labels.extend(r.get("labels", []))
            label_counts = Counter(all_labels)
            top_labels = [l for l, _ in label_counts.most_common(4)]
            region = self._infer_region(top_labels)

            scene = Scene(era_range, [style], top_labels, region)
            for r in unclaimed:
                rid = r.get("id")
                if rid:
                    scene.releases.append(r)
                    used_releases.add(rid)
                    for a in r.get("artists", []):
                        scene.artists.add(a)

            if len(scene.releases) >= self.MIN_SCENE_SIZE:
                scenes.append(scene)

        scenes.sort(key=lambda s: len(s.releases), reverse=True)
        return scenes

    def build_label_tree(self, collection_labels: list[str],
                         discogs_service, data_dir: Path | None = None
                         ) -> dict[str, LabelFamily]:
        """Fetch Discogs label details for parent/sublabel relationships.

        Caches results in label_cache.json with 30-day TTL.
        """
        label_cache = self._load_label_cache(data_dir)
        result: dict[str, LabelFamily] = {}
        labels_to_fetch: list[str] = []

        for label_name in collection_labels[:30]:
            cached = label_cache.get(label_name)
            if cached and not self._is_cache_expired(cached):
                result[label_name] = LabelFamily(
                    name=cached["name"],
                    label_id=cached.get("label_id"),
                    parent_name=cached.get("parent_name", ""),
                    parent_id=cached.get("parent_id"),
                    siblings=cached.get("siblings", []),
                    sublabels=cached.get("sublabels", []),
                )
            else:
                labels_to_fetch.append(label_name)

        for label_name in labels_to_fetch:
            try:
                search_results = discogs_service.search(
                    query=label_name, type="label", per_page=1
                )
                if not search_results:
                    continue
                label_id = search_results[0].get("id")
                if not label_id:
                    continue

                label_obj = discogs_service.client.label(label_id)

                parent_name = ""
                parent_id = None
                siblings = []
                sublabels_list = []

                try:
                    if hasattr(label_obj, 'parent_label') and label_obj.parent_label:
                        parent_name = label_obj.parent_label.name
                        parent_id = label_obj.parent_label.id
                        try:
                            parent_obj = discogs_service.client.label(parent_id)
                            siblings = [
                                sl.name for sl in parent_obj.sublabels
                                if sl.name != label_name
                            ][:10]
                        except Exception:
                            pass
                except Exception:
                    pass

                try:
                    if hasattr(label_obj, 'sublabels'):
                        sublabels_list = [
                            sl.name for sl in label_obj.sublabels
                        ][:10]
                except Exception:
                    pass

                family = LabelFamily(
                    name=label_name,
                    label_id=label_id,
                    parent_name=parent_name,
                    parent_id=parent_id,
                    siblings=siblings,
                    sublabels=sublabels_list,
                )
                result[label_name] = family
                label_cache[label_name] = {
                    "name": label_name,
                    "label_id": label_id,
                    "parent_name": parent_name,
                    "parent_id": parent_id,
                    "siblings": siblings,
                    "sublabels": sublabels_list,
                    "fetched_at": datetime.now().isoformat(),
                }

                time.sleep(1.1)  # rate limit

            except Exception as e:
                logger.warning("Failed to fetch label info for '%s': %s",
                               label_name, e)
                continue

        if labels_to_fetch:
            self._save_label_cache(label_cache, data_dir)

        return result

    def get_scene_summary_for_prompt(self, scenes: list[Scene],
                                     max_scenes: int = 8) -> str:
        """Format scenes for inclusion in Claude prompt."""
        if not scenes:
            return ""
        lines = [
            "COLLECTION SCENES (clusters of related releases "
            "in the listener's collection):"
        ]
        for scene in scenes[:max_scenes]:
            lines.append(
                f"  - {scene.summary} [{len(scene.releases)} releases]")
        return "\n".join(lines)

    def get_label_tree_for_prompt(
        self, label_families: dict[str, LabelFamily],
        max_labels: int = 10
    ) -> str:
        """Format label genealogy for Claude prompt."""
        if not label_families:
            return ""
        lines = [
            "LABEL FAMILY TREE (the listener's labels "
            "and their relationships):"
        ]
        for name, fam in list(label_families.items())[:max_labels]:
            parts = [f"  - {name}"]
            if fam.parent_name:
                parts.append(f"parent: {fam.parent_name}")
            if fam.siblings:
                parts.append(
                    f"siblings: {', '.join(fam.siblings[:5])}")
            if fam.sublabels:
                parts.append(
                    f"sublabels: {', '.join(fam.sublabels[:5])}")
            line = " -> ".join(parts) if len(parts) > 1 else parts[0]
            lines.append(line)
        return "\n".join(lines)

    def _get_year(self, release: dict) -> int | None:
        y = release.get("year")
        if isinstance(y, int) and 1900 <= y <= 2099:
            return y
        if isinstance(y, str):
            try:
                yi = int(y[:4])
                if 1900 <= yi <= 2099:
                    return yi
            except (ValueError, TypeError):
                pass
        return None

    def _infer_region(self, labels: list[str]) -> str:
        for label in labels:
            if label in LABEL_REGION_MAP:
                return LABEL_REGION_MAP[label]
        return ""

    def _load_label_cache(self, data_dir: Path | None) -> dict:
        if not data_dir:
            return {}
        cache_file = data_dir / "label_cache.json"
        if not cache_file.exists():
            return {}
        try:
            raw = cache_file.read_text(encoding="utf-8")
            if len(raw) > 2 * 1024 * 1024:
                return {}
            return json.loads(raw)
        except (json.JSONDecodeError, IOError):
            return {}

    def _is_cache_expired(self, entry: dict) -> bool:
        fetched = entry.get("fetched_at", "")
        if not fetched:
            return True
        try:
            fetched_dt = datetime.fromisoformat(fetched)
            return (datetime.now() - fetched_dt
                    > timedelta(days=self.LABEL_CACHE_TTL_DAYS))
        except (ValueError, TypeError):
            return True

    def _save_label_cache(self, cache_data: dict,
                          data_dir: Path | None) -> None:
        if not data_dir:
            return
        from services.thumbs import _atomic_write_json
        cache_file = data_dir / "label_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(cache_file, cache_data)
