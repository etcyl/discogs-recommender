#!/usr/bin/env python3
"""Check whether recommended tracks actually exist.

Structural metrics (artist diversity, decade spread) say nothing about whether
a model invented the song. This resolves every pick in a saved bench run
against public music catalogues and reports a hallucination rate.

The matching and lookup logic lives in services/verification — the same code
the running app uses to flag or drop unverifiable recommendations — so what
this measures is exactly what the app enforces.

  python tools/verify_runs.py                # every run in bench/runs
  python tools/verify_runs.py --match schema
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services import verification  # noqa: E402
from services.verification import Status  # noqa: E402

RUNS_DIR = BASE_DIR / "bench" / "runs"
OUT = BASE_DIR / "bench" / "verification.md"


def load_runs(match: str) -> list[tuple[Path, dict]]:
    runs = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        if match and match not in path.stem:
            continue
        try:
            runs.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            print(f"  skipping unreadable run: {path.name}")
    return runs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--match", default="")
    p.add_argument("-o", "--out", type=Path, default=OUT)
    p.add_argument("--write-back", action="store_true",
                   help="store the verification result into the run JSON")
    args = p.parse_args()

    runs = load_runs(args.match)
    if not runs:
        print("no runs found")
        return 1

    lines = [
        "# Do the recommended songs actually exist?\n",
        "Every pick resolved against Deezer, then iTunes, then MusicBrainz, "
        "using the same `services/verification` code the app itself runs. "
        "A track is **confirmed** when a catalogue returns a close artist+title "
        "match. **Unconfirmed** means no catalogue had it — usually invented, "
        "occasionally just too obscure to be indexed.\n",
    ]
    summary, details = [], []

    for path, run in runs:
        provider = run["provider"]
        songs = run.get("songs", [])
        print(f"{provider} — {len(songs)} tracks")

        checked, _ = verification.verify_songs(
            [dict(s) for s in songs], verification.Policy.FLAG.value)

        confirmed, unconfirmed = 0, []
        for song in checked:
            status = song["verification"]["status"]
            if status in (Status.VERIFIED.value, Status.CORRECTED.value):
                confirmed += 1
            else:
                unconfirmed.append(song)
            print(f"  {'ok  ' if status in ('verified', 'corrected') else 'MISS'} "
                  f"{song.get('artist')} — {song.get('title')}")

        total = len(songs)
        rate = round(100 * confirmed / total, 1) if total else 0.0
        summary.append((provider, total, confirmed, len(unconfirmed), rate))

        details.append(f"\n### `{provider}` — {len(unconfirmed)} unconfirmed of {total}\n")
        if unconfirmed:
            details.extend(
                f"- {s.get('artist')} — {s.get('title')} ({s.get('year', '?')})"
                for s in unconfirmed)
        else:
            details.append("_Every pick resolved to a real recording._")

        if args.write_back:
            run["verification"] = {
                "confirmed": confirmed, "unconfirmed": len(unconfirmed),
                "confirmed_pct": rate,
            }
            for original, annotated in zip(run["songs"], checked):
                original["verification"] = annotated["verification"]
            path.write_text(json.dumps(run, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    lines.append("## Summary\n")
    lines.append("| Provider | Tracks | Confirmed | Unconfirmed | Confirmed % |")
    lines.append("|---|---|---|---|---|")
    for provider, total, ok, miss, rate in sorted(summary, key=lambda r: -r[4]):
        lines.append(f"| `{provider}` | {total} | {ok} | {miss} | **{rate}%** |")

    lines.append("\n## Unconfirmed picks\n")
    lines.extend(details)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n-> {args.out}")
    for provider, total, ok, miss, rate in sorted(summary, key=lambda r: -r[4]):
        print(f"  {provider:28s} {rate:5.1f}% confirmed ({miss} unconfirmed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
