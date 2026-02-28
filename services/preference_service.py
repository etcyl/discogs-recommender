"""Preference Service - learns which match_attributes the user responds to.

Tracks positive (thumbs up), negative (thumbs down), and skip (< 30s) signals
for each match_attribute, building a preference profile that biases future
playlist generation and post-generation reranking.
"""

import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_PREF_ENTRIES = 50
SKIP_WEIGHT = 0.5


class PreferenceService:

    def record_positive(self, match_attributes: list[str],
                        data_dir: Path | None = None) -> None:
        """Record a thumbs-up: increment positive count for each attribute."""
        if not match_attributes:
            return
        prefs = self._load_prefs(data_dir)
        for attr in match_attributes:
            attr = attr.strip().lower()
            if not attr:
                continue
            if attr not in prefs:
                prefs[attr] = {"positive": 0, "negative": 0, "skip": 0}
            prefs[attr]["positive"] += 1
        self._recalc_scores(prefs)
        self._save_prefs(prefs, data_dir)

    def record_negative(self, match_attributes: list[str],
                        data_dir: Path | None = None) -> None:
        """Record a thumbs-down: increment negative count for each attribute."""
        if not match_attributes:
            return
        prefs = self._load_prefs(data_dir)
        for attr in match_attributes:
            attr = attr.strip().lower()
            if not attr:
                continue
            if attr not in prefs:
                prefs[attr] = {"positive": 0, "negative": 0, "skip": 0}
            prefs[attr]["negative"] += 1
        self._recalc_scores(prefs)
        self._save_prefs(prefs, data_dir)

    def record_skip(self, match_attributes: list[str],
                    data_dir: Path | None = None) -> None:
        """Record an early skip (< 30s): increment skip count (half weight)."""
        if not match_attributes:
            return
        prefs = self._load_prefs(data_dir)
        for attr in match_attributes:
            attr = attr.strip().lower()
            if not attr:
                continue
            if attr not in prefs:
                prefs[attr] = {"positive": 0, "negative": 0, "skip": 0}
            prefs[attr]["skip"] += 1
        self._recalc_scores(prefs)
        self._save_prefs(prefs, data_dir)

    def get_preference_profile(self, data_dir: Path | None = None) -> dict:
        """Return the full preference profile."""
        return self._load_prefs(data_dir)

    def get_preference_summary_for_prompt(self,
                                          data_dir: Path | None = None) -> str:
        """Format preference profile for inclusion in Claude prompt.

        Only includes attributes with sufficient signal (5+ total interactions).
        """
        prefs = self._load_prefs(data_dir)
        if not prefs:
            return ""

        meaningful = {}
        for attr, data in prefs.items():
            total = data["positive"] + data["negative"] + data["skip"]
            if total >= 5:
                meaningful[attr] = data

        if not meaningful:
            return ""

        sorted_attrs = sorted(meaningful.items(),
                              key=lambda x: abs(x[1].get("score", 0)),
                              reverse=True)

        preferred = []
        avoided = []
        for attr, data in sorted_attrs:
            score = data.get("score", 0)
            total = data["positive"] + data["negative"] + data["skip"]
            if score > 0.2:
                preferred.append(
                    f"{attr} (score: +{score:.2f}, {total} interactions)")
            elif score < -0.2:
                avoided.append(
                    f"{attr} (score: {score:.2f}, {total} interactions)")

        lines = [
            "LISTENER ATTRIBUTE PREFERENCES (learned from listening behavior):"
        ]
        if preferred:
            lines.append("  PREFERRED connection types (lean into these):")
            for p in preferred[:6]:
                lines.append(f"    + {p}")
        if avoided:
            lines.append("  AVOIDED connection types (de-emphasize these):")
            for a in avoided[:6]:
                lines.append(f"    - {a}")

        return "\n".join(lines) if len(lines) > 1 else ""

    def get_attribute_score(self, attr: str,
                            data_dir: Path | None = None) -> float:
        """Return the preference score for a single attribute. 0.0 if unknown."""
        prefs = self._load_prefs(data_dir)
        attr_data = prefs.get(attr.strip().lower())
        if not attr_data:
            return 0.0
        return attr_data.get("score", 0.0)

    def compute_preference_bonus(self, match_attributes: list[str],
                                 data_dir: Path | None = None) -> float:
        """Compute aggregate preference bonus for a list of attributes.

        Used for post-generation reranking. Returns a value typically in
        [-1, +1].
        """
        if not match_attributes:
            return 0.0
        prefs = self._load_prefs(data_dir)
        total_score = 0.0
        count = 0
        for attr in match_attributes:
            attr_data = prefs.get(attr.strip().lower())
            if attr_data and attr_data.get("score") is not None:
                total_score += attr_data["score"]
                count += 1
        return total_score / max(count, 1)

    # --- Private helpers ---

    def _recalc_scores(self, prefs: dict) -> None:
        """Recalculate score for all attributes.

        score = (positive - (negative + skip * 0.5)) /
                (positive + negative + skip * 0.5)
        Range: -1.0 to +1.0
        """
        for attr, data in prefs.items():
            pos = data.get("positive", 0)
            neg = data.get("negative", 0)
            skip = data.get("skip", 0)
            effective_neg = neg + skip * SKIP_WEIGHT
            total = pos + effective_neg
            if total == 0:
                data["score"] = 0.0
            else:
                data["score"] = round((pos - effective_neg) / total, 4)

    def _load_prefs(self, data_dir: Path | None) -> dict:
        """Load attribute preferences from disk."""
        pref_file = self._pref_file(data_dir)
        if not pref_file or not pref_file.exists():
            return {}
        try:
            raw = pref_file.read_text(encoding="utf-8")
            if len(raw) > 1024 * 1024:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_prefs(self, prefs: dict, data_dir: Path | None) -> None:
        """Save attribute preferences to disk."""
        pref_file = self._pref_file(data_dir)
        if not pref_file:
            return
        from services.thumbs import _atomic_write_json
        pref_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(pref_file, prefs)

    def _pref_file(self, data_dir: Path | None) -> Path | None:
        """Return path to attribute_prefs.json."""
        if not data_dir:
            return None
        return data_dir / "attribute_prefs.json"
