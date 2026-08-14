#!/usr/bin/env python3
"""Screen a candidate playlist before it becomes a channel.

Three gates, in order of how cheap they are:

  1. Exclusions — artists the listener already has. Reads their Discogs
     collection, their imported playlists, and any previously built channel,
     so "recommend me something new" actually means new.
  2. Duplicates — within the candidate list itself.
  3. Existence — every surviving track against the music catalogues, using the
     same services/verification the app runs.

Writes a cleaned list alongside the input, and prints what it dropped and why.

  python tools/check_candidates.py bench/seeds/etcyl_1000.txt
  python tools/check_candidates.py list.txt --no-verify     # gates 1-2 only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services import paths, verification  # noqa: E402
from services.verification import Status, normalize  # noqa: E402
from tools.build_channel import load_sectioned  # noqa: E402

SEEDS = BASE_DIR / "bench" / "seeds"


def artist_key(name: str) -> str:
    """Normalised artist name, with a leading 'the' dropped."""
    n = normalize(name)
    return n[4:] if n.startswith("the ") else n


def collect_exclusions() -> tuple[set[str], dict[str, str]]:
    """Every artist the listener already has, and where each came from."""
    excluded: set[str] = set()
    origin: dict[str, str] = {}

    def add(name: str, source: str):
        k = artist_key(name)
        if k and k not in excluded:
            excluded.add(k)
            origin[k] = source

    # Saved seeds: Discogs collections and imported playlists.
    for path in SEEDS.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        label = data.get("name") or path.stem
        for item in data.get("items", []):
            for a in item.get("artists", []) or []:
                add(a, label)
            if item.get("artist"):
                add(item["artist"], label)

    # Channels already built — don't re-recommend what was just recommended.
    root = paths.data_dir()
    if root.exists():
        for cf in root.glob("*/channels.json"):
            try:
                channels = json.loads(cf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for ch in channels:
                for t in (ch.get("source_data") or {}).get("tracks", []) or []:
                    if t.get("artist"):
                        add(t["artist"], f"channel: {ch.get('name', '?')}")

    # Plain-text seed lists.
    for path in SEEDS.glob("*.txt"):
        for t in load_sectioned(path):
            if t.get("artist"):
                add(t["artist"], path.stem)

    return excluded, origin


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("candidates", type=Path)
    p.add_argument("-o", "--out", type=Path)
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--allow", action="append", default=[],
                   help="artist to permit despite being excluded (repeatable)")
    args = p.parse_args()

    tracks = load_sectioned(args.candidates)
    print(f"{len(tracks)} candidates from {args.candidates.name}")

    excluded, origin = collect_exclusions()
    for a in args.allow:
        excluded.discard(artist_key(a))
    # The file being screened is itself in seeds/ — don't exclude against itself.
    own = {artist_key(t["artist"]) for t in tracks if t.get("artist")}
    self_only = {k for k in own if origin.get(k, "").startswith(args.candidates.stem)}
    excluded -= self_only
    print(f"{len(excluded)} artists already in the listener's library\n")

    kept, dropped_excluded, dropped_dupe, dropped_junk = [], [], [], []
    seen: set[tuple[str, str]] = set()
    seen_artists: dict[str, int] = {}

    def is_wellformed(t: dict) -> bool:
        """Reject notes and placeholders that a hand-written list accumulates.

        A candidate list is drafted by a person or a model, and drafts contain
        working notes ("X is excluded, skip") and unfinished entries
        ("Some Band - ?"). Those parse as tracks and would otherwise sail
        through — a "?" title verifies as SKIPPED, not UNVERIFIED, so the
        catalogue gate does not catch it.
        """
        artist, title = t.get("artist", "").strip(), t.get("title", "").strip()
        if not artist or not title:
            return False
        if title in {"?", "-", "..."} or artist in {"?", "-"}:
            return False
        blob = f"{artist} {title}".lower()
        if any(w in blob for w in (" skip", "skip ", "excluded", "todo", "tbd")):
            return False
        return True

    for t in tracks:
        if not is_wellformed(t):
            dropped_junk.append(t)
            continue
        akey = artist_key(t.get("artist", ""))
        if akey in excluded:
            dropped_excluded.append((t, origin.get(akey, "?")))
            continue
        key = (akey, normalize(t.get("title", "")))
        if key in seen:
            dropped_dupe.append(t)
            continue
        seen.add(key)
        seen_artists[akey] = seen_artists.get(akey, 0) + 1
        kept.append(t)

    if dropped_junk:
        print(f"-- not well-formed entries: {len(dropped_junk)}")
        for t in dropped_junk[:10]:
            print(f"   {t.get('artist','')!r} / {t.get('title','')!r}")
        if len(dropped_junk) > 10:
            print(f"   ...and {len(dropped_junk) - 10} more")
        print()

    if dropped_excluded:
        print(f"-- already in the library: {len(dropped_excluded)}")
        for t, src in dropped_excluded[:20]:
            print(f"   {t['artist']} — {t['title']}   (from {src})")
        if len(dropped_excluded) > 20:
            print(f"   ...and {len(dropped_excluded) - 20} more")
    if dropped_dupe:
        print(f"\n-- duplicates within this list: {len(dropped_dupe)}")
        for t in dropped_dupe[:15]:
            print(f"   {t['artist']} — {t['title']}")

    over = {a: n for a, n in seen_artists.items() if n > 2}
    if over:
        print(f"\n-- artists appearing more than twice: {len(over)}")
        for a, n in sorted(over.items(), key=lambda kv: -kv[1])[:10]:
            print(f"   {a}: {n}")

    print(f"\n{len(kept)} candidates survive the exclusion gates")

    if not args.no_verify and kept:
        print("\nFact-checking against Deezer / iTunes / MusicBrainz...")
        checked, summary = verification.verify_songs(
            kept, verification.Policy.FLAG.value)
        unconfirmed = [t for t in checked
                       if t["verification"]["status"] == Status.UNVERIFIED.value]
        print(f"  confirmed    {summary['verified']}")
        print(f"  name differs {summary['corrected']}")
        print(f"  unconfirmed  {summary['unverified']}")
        rate = 100 * (len(checked) - len(unconfirmed)) / len(checked)
        print(f"  -> {rate:.1f}% real")
        if unconfirmed:
            print("\n-- could not be confirmed (dropped):")
            for t in unconfirmed:
                print(f"   {t['artist']} — {t['title']}")
        kept = [t for t in checked
                if t["verification"]["status"] != Status.UNVERIFIED.value]

    out = args.out or args.candidates.with_name(args.candidates.stem + "_clean.txt")
    lines, section = [], None
    for t in kept:
        if t.get("section") != section:
            section = t.get("section")
            lines.append(f"\n# --- {section} ---")
        lines.append(f"{t['artist']} - {t['title']}")
    out.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    print(f"\n{len(kept)} clean candidates -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
