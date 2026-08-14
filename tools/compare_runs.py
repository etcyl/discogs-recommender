#!/usr/bin/env python3
"""Compare saved playlist_bench runs side by side.

Reads bench/runs/*.json and writes bench/comparison.md — a metrics table,
a per-provider overlap matrix, and the songs only one provider found.

  python tools/compare_runs.py                 # every run
  python tools/compare_runs.py --match v2      # only runs whose id contains "v2"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = BASE_DIR / "bench" / "runs"
OUT = BASE_DIR / "bench" / "comparison.md"


def load_runs(match: str) -> list[dict]:
    runs = []
    for p in sorted(RUNS_DIR.glob("*.json")):
        if match and match not in p.stem:
            continue
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"  skipping unreadable run: {p.name}")
    return runs


def key_set(run: dict) -> set[tuple[str, str]]:
    return {((s.get("artist") or "").strip().lower(),
             (s.get("title") or "").strip().lower())
            for s in run.get("songs", []) if s.get("artist") or s.get("title")}


def _fmt(v, dash="—"):
    return dash if v is None else str(v)


def build_report(runs: list[dict]) -> str:
    if not runs:
        return "No runs found.\n"

    lines = ["# Playlist generation — provider comparison\n"]
    seed = runs[0].get("seed", {})
    lines.append(f"Seed: **{seed.get('name','?')}** "
                 f"({seed.get('kind','?')}, {seed.get('size','?')} items)\n")

    # --- metrics table -------------------------------------------------
    cols = [
        ("count", "Songs"),
        ("seconds", "Time (s)"),
        ("unique_artists", "Artists"),
        ("artist_diversity", "Artist div."),
        ("unique_decades", "Decades"),
        ("missing_year", "No year"),
        ("seed_artist_reuse", "Seed reuse"),
        ("seed_track_leakage", "Seed leak"),
        ("internal_duplicates", "Dupes"),
        ("field_completeness", "Fields OK"),
        ("mean_obscurity", "Obscurity"),
        ("mean_reason_len", "Reason len"),
    ]
    lines.append("## Metrics\n")
    lines.append("| Provider | " + " | ".join(h for _, h in cols) + " |")
    lines.append("|---" * (len(cols) + 1) + "|")
    for r in runs:
        m = dict(r.get("metrics", {}))
        m["seconds"] = r.get("seconds")
        lines.append(f"| `{r['provider']}` | "
                     + " | ".join(_fmt(m.get(k)) for k, _ in cols) + " |")

    lines.append("\n**Reading the table.** `Artist div.` is unique artists / songs "
                 "(1.0 = no artist repeats). `Seed reuse` is the share of picks by an "
                 "artist already in the seed. `Seed leak` counts songs copied straight "
                 "from the seed — should always be 0. `Fields OK` is the share of songs "
                 "with artist, title and year all present.\n")

    # --- overlap matrix -------------------------------------------------
    lines.append("## Overlap\n")
    lines.append("Shared song picks between providers (song-level, case-insensitive).\n")
    names = [r["provider"] for r in runs]
    sets = [key_set(r) for r in runs]
    lines.append("| | " + " | ".join(f"`{n}`" for n in names) + " |")
    lines.append("|---" * (len(names) + 1) + "|")
    for i, n in enumerate(names):
        row = []
        for j in range(len(names)):
            row.append("—" if i == j else str(len(sets[i] & sets[j])))
        lines.append(f"| `{n}` | " + " | ".join(row) + " |")

    # --- artist-level overlap -------------------------------------------
    art_sets = [{a for a, _ in s} for s in sets]
    lines.append("\nArtist-level overlap:\n")
    lines.append("| | " + " | ".join(f"`{n}`" for n in names) + " |")
    lines.append("|---" * (len(names) + 1) + "|")
    for i, n in enumerate(names):
        row = ["—" if i == j else str(len(art_sets[i] & art_sets[j]))
               for j in range(len(names))]
        lines.append(f"| `{n}` | " + " | ".join(row) + " |")

    # --- unique picks ---------------------------------------------------
    lines.append("\n## Picks only one provider made\n")
    for i, r in enumerate(runs):
        others = set().union(*(art_sets[:i] + art_sets[i + 1:])) if len(runs) > 1 else set()
        uniq = [s for s in r.get("songs", [])
                if (s.get("artist") or "").strip().lower() not in others]
        lines.append(f"\n### `{r['provider']}` — {len(uniq)} unique artists\n")
        for s in uniq[:15]:
            lines.append(f"- **{s.get('artist','?')} — {s.get('title','?')}** "
                         f"({s.get('year','?')})")
        if len(uniq) > 15:
            lines.append(f"- _…and {len(uniq) - 15} more_")

    # --- consensus ------------------------------------------------------
    if len(runs) > 1:
        counts: dict[str, int] = {}
        for s in art_sets:
            for a in s:
                counts[a] = counts.get(a, 0) + 1
        consensus = sorted((a for a, c in counts.items() if c == len(runs)))
        lines.append(f"\n## Picked by every provider ({len(consensus)} artists)\n")
        lines.append(", ".join(a.title() for a in consensus) if consensus
                     else "_None — the providers agreed on nothing._")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--match", default="", help="only runs whose filename contains this")
    p.add_argument("-o", "--out", type=Path, default=OUT)
    args = p.parse_args()

    runs = load_runs(args.match)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_report(runs), encoding="utf-8")
    print(f"{len(runs)} runs -> {args.out}")
    for r in runs:
        m = r.get("metrics", {})
        print(f"  {r['provider']:28s} {m.get('count', 0):3d} songs  "
              f"{m.get('unique_artists', 0):3d} artists  "
              f"{_fmt(r.get('seconds')):>6s}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
