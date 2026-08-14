#!/usr/bin/env python3
"""Turn a text playlist into a ready-to-play radio channel.

Reads "Artist - Title" lines, fact-checks every track against the music
catalogues, resolves each one to a YouTube video, and writes an `upload`
channel in `play_playlist` mode. Because the video IDs are resolved here,
the channel starts playing immediately instead of searching YouTube for a
hundred songs while you wait.

  python tools/build_channel.py bench/seeds/etcyl_100.txt --name "Sunday Mix"
  python tools/build_channel.py list.txt --dry-run     # verify only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services import channel_service, paths, verification  # noqa: E402
from services.radio_service import RadioService  # noqa: E402
from services.verification import Status  # noqa: E402
from tools.playlist_bench import _TRACK_LINE  # noqa: E402


def load_sectioned(path: Path) -> list[dict]:
    """Parse 'Artist - Title' lines, remembering the `# --- Section ---` above.

    The section is what makes interleaving possible — without it a file
    grouped by genre plays sixteen indie rock songs and then twelve house
    tracks, which is a worse listen than the same songs shuffled between.
    """
    tracks, section = [], ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            label = line.lstrip("#").strip().strip("-").strip()
            if label:
                section = label
            continue
        if not line:
            continue
        m = _TRACK_LINE.match(line)
        if m:
            tracks.append({"artist": m.group("artist").strip(),
                           "title": m.group("title").strip(),
                           "section": section})
    return tracks


def interleave(tracks: list[dict]) -> list[dict]:
    """Round-robin across sections so consecutive songs change texture."""
    buckets: dict[str, list[dict]] = {}
    for t in tracks:
        buckets.setdefault(t.get("section", ""), []).append(t)

    order = list(buckets)
    out, i = [], 0
    while any(buckets.values()):
        bucket = buckets[order[i % len(order)]]
        if bucket:
            out.append(bucket.pop(0))
        i += 1
    return out


def find_user_dir(explicit: str | None) -> Path:
    """Locate the per-user data directory the app writes channels into."""
    if explicit:
        return Path(explicit)
    root = paths.data_dir()
    candidates = [p for p in root.iterdir()
                  if p.is_dir() and (p / "channels.json").exists()] if root.exists() else []
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    # No channels written yet — fall back to the single admin directory.
    from services import auth_service
    admin = auth_service.get_admin_user()
    if not admin:
        raise SystemExit("No user found. Start the app once so it creates one.")
    return root / admin["id"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tracks", type=Path, help="text file, 'Artist - Title' per line")
    p.add_argument("--name", default="", help="channel name (default: file stem)")
    p.add_argument("--user-dir", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="verify and report, write nothing")
    p.add_argument("--keep-unconfirmed", action="store_true",
                   help="keep tracks no catalogue could confirm")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--limit", type=int, default=0,
                   help="cap the playlist at this many tracks")
    p.add_argument("--no-interleave", action="store_true",
                   help="keep file order instead of alternating between sections")
    args = p.parse_args()

    tracks = load_sectioned(args.tracks)
    if not tracks:
        raise SystemExit(f"No tracks parsed from {args.tracks}")
    print(f"{len(tracks)} tracks from {args.tracks.name}\n")

    # --- 1. fact-check ----------------------------------------------------
    if not args.no_verify:
        print("Fact-checking against Deezer / iTunes / MusicBrainz...")
        checked, summary = verification.verify_songs(
            tracks, verification.Policy.FLAG.value)
        unconfirmed = [t for t in checked
                       if t["verification"]["status"] == Status.UNVERIFIED.value]
        corrected = [t for t in checked
                     if t["verification"]["status"] == Status.CORRECTED.value]

        print(f"  confirmed   {summary['verified']}")
        print(f"  name differs{summary['corrected']:>3}")
        print(f"  unconfirmed {summary['unverified']}")

        if corrected:
            # Reported, deliberately not applied. What a catalogue returns is
            # often a *different version* rather than a better spelling — the
            # Carl Craig remix of "Falling Up", the Erol Alkan rework of
            # "Forever Dolphin Love", "(2005 Remaster)". Adopting those would
            # quietly swap the track for one that wasn't asked for, and the
            # clean canonical title also searches better on YouTube.
            print("\n  Confirmed under a different name (keeping the original):")
            for t in corrected:
                v = t["verification"]
                print(f"    {t['artist']} — {t['title']}")
                print(f"      catalogue has: {v['matched_artist']} — {v['matched_title']}")

        if unconfirmed:
            print("\n  Unconfirmed:")
            for t in unconfirmed:
                print(f"    {t['artist']} — {t['title']}")
            if not args.keep_unconfirmed:
                tracks = [t for t in checked
                          if t["verification"]["status"] != Status.UNVERIFIED.value]
                print(f"\n  Dropped {len(unconfirmed)}; {len(tracks)} remain.")
            else:
                tracks = checked
        else:
            tracks = checked
            print("\n  Every track confirmed.")

    if not args.no_interleave:
        tracks = interleave(tracks)
        print("\n  Interleaved across "
              f"{len({t.get('section') for t in tracks})} sections.")

    if args.dry_run:
        for i, t in enumerate(tracks[:args.limit or len(tracks)], 1):
            print(f"  {i:3d}. {t['artist']} — {t['title']}")
        return 0

    # --- 2. resolve to YouTube -------------------------------------------
    print(f"\nResolving {len(tracks)} tracks to YouTube videos...")
    radio = RadioService()
    resolved = radio.resolve_youtube_ids(tracks)
    print(f"  {len(resolved)} playable, {len(tracks) - len(resolved)} with no match")

    # resolve_youtube_ids rewrites artist/title from the video title; where a
    # catalogue disagrees, put the catalogue's name back.
    fixed = verification.reconcile(resolved)
    if fixed:
        print(f"  reconciled {fixed} title(s) the video search had overwritten")

    if not resolved:
        raise SystemExit("Nothing resolved — is YouTube reachable?")

    if args.limit and len(resolved) > args.limit:
        resolved = resolved[:args.limit]
        print(f"  capped at {args.limit}")

    # --- 3. write the channel --------------------------------------------
    user_dir = find_user_dir(args.user_dir)
    user_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or args.tracks.stem.replace("_", " ").title()

    payload = [{
        "artist": s.get("artist", ""),
        "title": s.get("title", ""),
        "album": s.get("album", ""),
        "year": s.get("year", ""),
        "videoId": s.get("videoId", ""),
        "thumbnail": s.get("thumbnail", ""),
        "albumArt": s.get("albumArt", ""),
        "duration": s.get("duration", ""),
        "verification": s.get("verification", {}),
    } for s in resolved]

    existing = [c for c in channel_service.load_channels(data_dir=user_dir)
                if c.get("name") == name]
    for c in existing:
        try:
            channel_service.delete_channel(c["id"], data_dir=user_dir)
            print(f"  replaced existing channel {c['id']}")
        except ValueError:
            pass

    channel = channel_service.create_channel(
        name=name, source_type="upload", source_data={"tracks": payload},
        mode="play_playlist", num_songs=min(100, len(payload)),
        data_dir=user_dir)

    print(f"\nChannel '{channel['name']}' ({channel['id']}) — {len(payload)} tracks")
    print(f"  {user_dir / 'channels.json'}")
    print("\nOpen http://localhost:8000/radio and pick it from the sidebar.")

    out = BASE_DIR / "bench" / f"channel_{channel['id']}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  playlist also saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
