import random
from collections import Counter


class CollectionAnalyzer:
    def __init__(self, collection: list[dict]):
        self.collection = collection
        self.genre_counts = Counter()
        self.style_counts = Counter()
        self.label_counts = Counter()
        self.artist_counts = Counter()
        self.release_ids = set()
        self.owned_titles = set()  # normalized "artist - title" for fuzzy matching
        self._analyze()

    def _analyze(self):
        for release in self.collection:
            self.release_ids.add(release["id"])
            # Build normalized title keys for matching across ID types
            for artist in release.get("artists", []):
                key = f"{artist} - {release.get('title', '')}".lower().strip()
                self.owned_titles.add(key)
            for genre in release.get("genres", []):
                self.genre_counts[genre] += 1
            for style in release.get("styles", []):
                self.style_counts[style] += 1
            for label in release.get("labels", []):
                self.label_counts[label] += 1
            for artist in release.get("artists", []):
                self.artist_counts[artist] += 1

    def _is_owned(self, candidate: dict) -> bool:
        """Check if a candidate is already in the collection by ID or artist+title."""
        if candidate.get("id") in self.release_ids:
            return True
        # Also check by artist + title (handles master vs release ID mismatch)
        for artist in candidate.get("artists", []):
            key = f"{artist} - {candidate.get('title', '')}".lower().strip()
            if key in self.owned_titles:
                return True
        return False

    def get_profile(self) -> dict:
        return {
            "total_releases": len(self.collection),
            "top_genres": self.genre_counts.most_common(10),
            "top_styles": self.style_counts.most_common(15),
            "top_labels": self.label_counts.most_common(10),
            "top_artists": self.artist_counts.most_common(10),
        }

    def score_release(self, candidate: dict, discovery: int = 0) -> float:
        """Score a candidate release based on collection profile overlap.

        Higher discovery values reduce the weight of familiar artists and
        add random jitter to push diverse results higher.
        """
        if self._is_owned(candidate):
            return -1

        score = 0.0
        max_genre = self.genre_counts.most_common(1)[0][1] if self.genre_counts else 1
        max_style = self.style_counts.most_common(1)[0][1] if self.style_counts else 1
        max_label = self.label_counts.most_common(1)[0][1] if self.label_counts else 1

        # Scale down artist bonus at high discovery so new artists can surface
        artist_weight = max(1, 5 - (discovery / 25))  # 5 at 0, 1 at 100

        for artist in candidate.get("artists", []):
            if artist in self.artist_counts:
                score += artist_weight * min(self.artist_counts[artist], 3)

        for genre in candidate.get("genres", []):
            if genre in self.genre_counts:
                score += 3 * (self.genre_counts[genre] / max_genre)

        for style in candidate.get("styles", []):
            if style in self.style_counts:
                score += 4 * (self.style_counts[style] / max_style)

        for label in candidate.get("labels", []):
            if label in self.label_counts:
                score += 2 * (self.label_counts[label] / max_label)

        # Apply random jitter scaled by discovery level
        if discovery > 0:
            jitter = discovery / 100  # 0.0 – 1.0
            score *= random.uniform(1 - jitter * 0.5, 1 + jitter)

        return score

    def get_recommendations(self, discogs_service, max_results: int = 20,
                            discovery: int = 30) -> list[dict]:
        """Search Discogs for releases matching profile traits, score and rank them.

        discovery (0-100): controls how adventurous the results are.
        Low = safe favorites. High = broader, more surprising picks.
        """
        candidates = []
        seen_ids = set()

        def _add_candidates(results):
            for r in results:
                rid = r.get("id")
                if rid and rid not in seen_ids and not self._is_owned(r):
                    candidates.append(r)
                    seen_ids.add(rid)

        # Scale search breadth by discovery level
        d = discovery / 100  # 0.0 – 1.0
        n_styles = int(5 + d * (len(self.style_counts) - 5))  # 5 to all
        n_artists = int(5 + d * max(0, len(self.artist_counts) - 5))
        n_labels = int(3 + d * max(0, len(self.label_counts) - 3))

        styles_to_search = [s for s, _ in self.style_counts.most_common(n_styles)]
        artists_to_search = [a for a, _ in self.artist_counts.most_common(n_artists)]
        labels_to_search = [la for la, _ in self.label_counts.most_common(n_labels)]

        # Shuffle to get different API result pages each time
        random.shuffle(styles_to_search)
        random.shuffle(artists_to_search)
        random.shuffle(labels_to_search)

        # Pick a random page offset for variety
        def _rand_page():
            return random.randint(1, 3) if discovery > 20 else 1

        # Search by styles
        for style in styles_to_search:
            try:
                results = discogs_service.search(
                    style=style, type="release", per_page=20, page=_rand_page())
                _add_candidates(results)
            except Exception:
                continue

        # Search by artists
        for artist in artists_to_search:
            try:
                results = discogs_service.search(
                    artist=artist, type="master", per_page=20, page=_rand_page())
                _add_candidates(results)
            except Exception:
                continue

        # Search by labels
        for label in labels_to_search:
            try:
                results = discogs_service.search(
                    label=label, type="release", per_page=20, page=_rand_page())
                _add_candidates(results)
            except Exception:
                continue

        # At medium-high discovery, also search by genres (broader than styles)
        if discovery > 40:
            genres_to_search = [g for g, _ in self.genre_counts.most_common()]
            random.shuffle(genres_to_search)
            for genre in genres_to_search[:5]:
                try:
                    results = discogs_service.search(
                        genre=genre, type="release", per_page=20, page=_rand_page())
                    _add_candidates(results)
                except Exception:
                    continue

        # Score and sort
        scored = []
        for c in candidates:
            s = self.score_release(c, discovery=discovery)
            if s > 0:
                c["score"] = round(s, 2)
                scored.append(c)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:max_results]
