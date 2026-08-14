#!/usr/bin/env python3
"""Playlist generation bench.

Runs the same seed (a Spotify/YouTube playlist, a Discogs collection, or a
plain text track list) through every available generation approach and saves
each result so they can be compared side by side.

Approaches ("providers"):

  ollama:<model>   Local model via Ollama. Free, no API key.
  claude-code      Writes the exact prompt the app would send to a file, for an
                   agent (Claude Code) to answer offline. No API key needed.
                   Re-run with --ingest to read the answer back in.
  claude-sonnet    Anthropic API (requires ANTHROPIC_API_KEY).
  claude-haiku     Anthropic API (requires ANTHROPIC_API_KEY).
  algorithmic      No LLM at all. Discogs-collection profile scoring only.

Examples
--------
  # Seed from a text file of "Artist - Title" lines, run two local models
  python tools/playlist_bench.py --tracks seeds/my_playlist.txt \\
      --provider ollama:llama3.1:8b --provider ollama:qwen3:30b-a3b -n 25

  # Seed from a public Spotify playlist
  python tools/playlist_bench.py --spotify https://open.spotify.com/playlist/XXXX \\
      --provider ollama:qwen3:30b-a3b

  # Emit a prompt for Claude Code to answer, then ingest the answer
  python tools/playlist_bench.py --tracks seeds/x.txt --provider claude-code
  python tools/playlist_bench.py --ingest bench/prompts/<run-id>.json \\
      --response bench/responses/<run-id>.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services.llm_provider import parse_llm_json  # noqa: E402
from services.radio_service import RadioService  # noqa: E402

BENCH_DIR = BASE_DIR / "bench"
RUNS_DIR = BENCH_DIR / "runs"
PROMPTS_DIR = BENCH_DIR / "prompts"
RESPONSES_DIR = BENCH_DIR / "responses"
SEEDS_DIR = BENCH_DIR / "seeds"


# ---------------------------------------------------------------------------
# Seed loading
# ---------------------------------------------------------------------------

_TRACK_LINE = re.compile(
    r"^\s*(?:\d+[\.\)]\s*)?(?P<artist>.+?)\s+(?:-|–|—|–|—)\s+(?P<title>.+?)\s*$"
)


def load_tracks_file(path: Path) -> list[dict]:
    """Parse a plain text playlist: one 'Artist - Title' per line.

    Blank lines and lines starting with '#' are ignored. Numeric list
    prefixes ('1.', '12)') are stripped.
    """
    tracks = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _TRACK_LINE.match(line)
        if m:
            tracks.append({"artist": m.group("artist").strip(),
                           "title": m.group("title").strip()})
        else:
            # No separator — treat the whole line as a title
            tracks.append({"artist": "", "title": line})
    return tracks


def load_spotify(url: str) -> tuple[list[dict], dict]:
    from services.spotify_service import SpotifyService
    svc = SpotifyService()
    pid = svc.parse_playlist_url(url)
    if not pid:
        raise SystemExit(f"Could not parse a playlist ID out of: {url}")
    info = svc.get_playlist_info(pid)
    return svc.get_playlist_tracks(pid), info


def load_youtube(url: str) -> tuple[list[dict], dict]:
    from services.youtube_playlist_service import YouTubePlaylistService
    svc = YouTubePlaylistService()
    return svc.get_playlist_tracks(url), svc.get_playlist_info(url)


def load_discogs(token: str, username: str) -> tuple[list[dict], dict]:
    from services.discogs_service import DiscogsService
    from services.recommendation import CollectionAnalyzer
    svc = DiscogsService("DiscogsRecommenderBench/1.0", token, username)
    collection = svc.get_full_collection()
    profile = CollectionAnalyzer(collection).get_profile()
    return collection, profile


# ---------------------------------------------------------------------------
# Prompt construction — reuses the app's own prompt builders
# ---------------------------------------------------------------------------

def build_prompt(seed, args, batch_size: int) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) exactly as the app would build it."""
    captured = {}

    class _Capture(RadioService):
        """Intercepts the prompts instead of calling an LLM."""

        def _batched_generate(self, build_prompts, num_songs, **kw):
            captured["prompts"] = build_prompts(batch_size, "")
            return []

    svc = _Capture(prompt_tier=args.prompt_tier)
    if seed["kind"] == "discogs":
        svc.generate_playlist(
            seed["profile"], seed["items"],
            discovery=args.discovery, num_songs=batch_size,
            ai_model=args.prompt_model, era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts,
        )
    else:
        svc.generate_playlist_from_tracks(
            seed["items"], mode=args.mode,
            discovery=args.discovery, num_songs=batch_size,
            ai_model=args.prompt_model, era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts,
        )
    return captured["prompts"]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def run_ollama(seed, args, model: str) -> dict:
    svc = RadioService(ollama_base_url=args.ollama_url, ollama_model=model,
                       prompt_tier=args.prompt_tier)
    t0 = time.perf_counter()
    if seed["kind"] == "discogs":
        songs = svc.generate_playlist(
            seed["profile"], seed["items"], discovery=args.discovery,
            num_songs=args.num_songs, ai_model="ollama",
            era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts)
    else:
        songs = svc.generate_playlist_from_tracks(
            seed["items"], mode=args.mode, discovery=args.discovery,
            num_songs=args.num_songs, ai_model="ollama",
            era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts)
    return {"songs": songs, "seconds": round(time.perf_counter() - t0, 1)}


def run_anthropic(seed, args, model: str) -> dict:
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise SystemExit(f"{model} needs ANTHROPIC_API_KEY in the environment.")
    svc = RadioService(anthropic_api_key=key, prompt_tier=args.prompt_tier)
    t0 = time.perf_counter()
    if seed["kind"] == "discogs":
        songs = svc.generate_playlist(
            seed["profile"], seed["items"], discovery=args.discovery,
            num_songs=args.num_songs, ai_model=model,
            era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts)
    else:
        songs = svc.generate_playlist_from_tracks(
            seed["items"], mode=args.mode, discovery=args.discovery,
            num_songs=args.num_songs, ai_model=model,
            era_from=args.era_from, era_to=args.era_to,
            prefer_deep_cuts=args.deep_cuts)
    return {"songs": songs, "seconds": round(time.perf_counter() - t0, 1)}


def run_claude_code(seed, args, run_id: str) -> Path:
    """Write the prompt to disk for an agent to answer offline."""
    system_text, user_text = build_prompt(seed, args, args.num_songs)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPTS_DIR / f"{run_id}.json"
    prompt_path.write_text(json.dumps({
        "run_id": run_id,
        "provider": "claude-code",
        "num_songs": args.num_songs,
        "seed": _seed_meta(seed),
        "args": _args_meta(args),
        "system_prompt": system_text,
        "user_prompt": user_text,
        "answer_to": str(RESPONSES_DIR / f"{run_id}.json"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Human/agent-readable copy alongside the JSON
    (PROMPTS_DIR / f"{run_id}.txt").write_text(
        f"=== SYSTEM ===\n{system_text}\n\n=== USER ===\n{user_text}\n",
        encoding="utf-8")
    return prompt_path


def run_algorithmic(seed, args) -> dict:
    """No-LLM baseline: score Discogs search candidates against the profile."""
    if seed["kind"] != "discogs":
        raise SystemExit("The algorithmic provider needs a --discogs seed "
                         "(it scores releases against a collection profile).")
    from services.discogs_service import DiscogsService
    from services.recommendation import CollectionAnalyzer

    svc = DiscogsService("DiscogsRecommenderBench/1.0", args.discogs_token,
                         args.discogs_username)
    analyzer = CollectionAnalyzer(seed["items"])
    t0 = time.perf_counter()
    recs = analyzer.get_recommendations(
        svc, max_results=args.num_songs, discovery=args.discovery,
        era_from=args.era_from, era_to=args.era_to)
    songs = [{
        "artist": (r.get("artists") or [""])[0],
        "title": r.get("title", ""),
        "album": r.get("title", ""),
        "year": r.get("year", ""),
        "reason": f"Profile overlap score {r.get('score')} "
                  f"(genres: {', '.join(r.get('genres', [])[:3])})",
        "match_score": min(100, int(r.get("score", 0) * 4)),
        "match_attributes": ["genre lineage"],
        "similar_to": [],
        "obscurity_score": "",
    } for r in recs]
    return {"songs": songs, "seconds": round(time.perf_counter() - t0, 1)}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _decade(song) -> str:
    try:
        y = int(str(song.get("year", "") or 0)[:4])
    except (ValueError, TypeError):
        return "?"
    return f"{(y // 10) * 10}s" if y else "?"


def score_playlist(songs: list[dict], seed_tracks: list[dict]) -> dict:
    """Structural quality metrics. These measure shape, not taste."""
    n = len(songs)
    if not n:
        return {"count": 0}

    artists = [(s.get("artist") or "").strip().lower() for s in songs]
    uniq_artists = len({a for a in artists if a})
    decades = [_decade(s) for s in songs]
    known_decades = [d for d in decades if d != "?"]

    seed_artists = {(t.get("artist") or "").strip().lower()
                    for t in seed_tracks if t.get("artist")}
    seed_keys = {((t.get("artist") or "").strip().lower(),
                  (t.get("title") or "").strip().lower()) for t in seed_tracks}
    picks = [(a, (s.get("title") or "").strip().lower())
             for a, s in zip(artists, songs)]

    def _nums(field):
        out = []
        for s in songs:
            try:
                out.append(int(s[field]))
            except (KeyError, TypeError, ValueError):
                pass
        return out

    obscurity = _nums("obscurity_score")
    match = _nums("match_score")
    reasons = [s.get("reason", "") or "" for s in songs]

    return {
        "count": n,
        "unique_artists": uniq_artists,
        "artist_diversity": round(uniq_artists / n, 3),
        "max_per_artist": max((artists.count(a) for a in set(artists) if a),
                              default=0),
        "unique_decades": len(set(known_decades)),
        "decade_spread": dict(sorted(
            {d: decades.count(d) for d in set(decades)}.items())),
        "missing_year": decades.count("?"),
        "seed_artist_reuse": round(
            sum(1 for a in artists if a in seed_artists) / n, 3),
        "seed_track_leakage": sum(1 for p in picks if p in seed_keys),
        "internal_duplicates": n - len(set(picks)),
        "mean_obscurity": round(sum(obscurity) / len(obscurity), 1) if obscurity else None,
        "mean_match_score": round(sum(match) / len(match), 1) if match else None,
        "has_reason": sum(1 for r in reasons if len(r) > 20),
        "mean_reason_len": round(sum(len(r) for r in reasons) / n, 1),
        "field_completeness": round(sum(
            1 for s in songs
            if s.get("artist") and s.get("title") and s.get("year")
        ) / n, 3),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _seed_meta(seed) -> dict:
    return {"kind": seed["kind"], "name": seed.get("name", ""),
            "size": len(seed["items"]),
            "sample": [f"{t.get('artist','')} - {t.get('title','')}"
                       for t in seed["items"][:10]]
            if seed["kind"] != "discogs" else
            [f"{', '.join(r.get('artists', []))} - {r.get('title','')}"
             for r in seed["items"][:10]]}


def _args_meta(args) -> dict:
    return {"mode": args.mode, "discovery": args.discovery,
            "num_songs": args.num_songs, "prompt_tier": args.prompt_tier,
            "era_from": args.era_from, "era_to": args.era_to,
            "deep_cuts": args.deep_cuts}


def save_run(run_id: str, provider: str, seed, args, result: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    songs = result["songs"]
    seed_tracks = seed["items"] if seed["kind"] != "discogs" else []
    record = {
        "run_id": run_id,
        "provider": provider,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": result.get("seconds"),
        "seed": _seed_meta(seed),
        "args": _args_meta(args),
        "metrics": score_playlist(songs, seed_tracks),
        "songs": songs,
    }
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                    encoding="utf-8")

    md = [f"# {provider} — {len(songs)} songs",
          f"\nSeed: **{seed.get('name','')}** ({seed['kind']}, {len(seed['items'])} items) · "
          f"discovery {args.discovery} · {result.get('seconds')}s\n"]
    for i, s in enumerate(songs, 1):
        year = s.get("year") or "?"
        md.append(f"{i}. **{s.get('artist','?')} — {s.get('title','?')}** "
                  f"({year})  \n   _{s.get('reason','')}_")
    path.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    return path


def ingest_response(prompt_path: Path, response_path: Path) -> Path:
    """Turn an agent-written answer into a normal bench run record."""
    spec = json.loads(prompt_path.read_text(encoding="utf-8"))
    raw = response_path.read_text(encoding="utf-8")
    songs = parse_llm_json(raw)
    if not songs:
        raise SystemExit(f"No JSON array could be parsed from {response_path}")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": spec["run_id"],
        "provider": spec["provider"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": None,
        "seed": spec["seed"],
        "args": spec["args"],
        "metrics": score_playlist(songs, []),
        "songs": songs,
    }
    path = RUNS_DIR / f"{spec['run_id']}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                    encoding="utf-8")

    md = [f"# {spec['provider']} — {len(songs)} songs\n"]
    for i, s in enumerate(songs, 1):
        md.append(f"{i}. **{s.get('artist','?')} — {s.get('title','?')}** "
                  f"({s.get('year','?')})  \n   _{s.get('reason','')}_")
    path.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("seed source (pick one)")
    src.add_argument("--tracks", type=Path, help="text file, 'Artist - Title' per line")
    src.add_argument("--spotify", help="public Spotify playlist URL")
    src.add_argument("--youtube", help="public YouTube playlist URL")
    src.add_argument("--discogs", action="store_true", help="use a Discogs collection")
    src.add_argument("--discogs-token", default="")
    src.add_argument("--discogs-username", default="")

    p.add_argument("--provider", action="append", default=[],
                   help="repeatable: ollama:<model> | claude-code | claude-sonnet "
                        "| claude-haiku | algorithmic")
    p.add_argument("-n", "--num-songs", type=int, default=25)
    p.add_argument("--mode", default="similar_songs",
                   choices=["similar_songs", "new_discoveries"])
    p.add_argument("--discovery", type=int, default=40)
    p.add_argument("--era-from", type=int)
    p.add_argument("--era-to", type=int)
    p.add_argument("--deep-cuts", action="store_true")
    p.add_argument("--prompt-tier", default="auto",
                   choices=["auto", "compact", "rich"],
                   help="'rich' gives local models the full curator prompt")
    p.add_argument("--prompt-model", default="claude-sonnet",
                   help="which model's prompt shape to emit for claude-code")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--label", default="", help="tag appended to run IDs")

    p.add_argument("--ingest", type=Path, help="a bench/prompts/<id>.json file")
    p.add_argument("--response", type=Path, help="the agent's JSON answer")

    args = p.parse_args()

    if args.ingest:
        if not args.response:
            return p.error("--ingest requires --response")
        out = ingest_response(args.ingest, args.response)
        print(f"ingested -> {out}")
        return 0

    # -- load seed --------------------------------------------------------
    if args.tracks:
        items = load_tracks_file(args.tracks)
        seed = {"kind": "tracks", "items": items, "name": args.tracks.stem}
    elif args.spotify:
        items, info = load_spotify(args.spotify)
        seed = {"kind": "spotify", "items": items,
                "name": info.get("name", "spotify playlist")}
    elif args.youtube:
        items, info = load_youtube(args.youtube)
        seed = {"kind": "youtube", "items": items,
                "name": info.get("name", "youtube playlist")}
    elif args.discogs:
        if not (args.discogs_token and args.discogs_username):
            return p.error("--discogs needs --discogs-token and --discogs-username")
        items, profile = load_discogs(args.discogs_token, args.discogs_username)
        seed = {"kind": "discogs", "items": items, "profile": profile,
                "name": f"{args.discogs_username} collection"}
    else:
        return p.error("pick a seed: --tracks / --spotify / --youtube / --discogs")

    if not seed["items"]:
        return p.error("seed is empty")

    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Snapshot the resolved seed so a run stays reproducible even if the
    # upstream playlist changes — but skip it when an identical snapshot
    # already exists, otherwise repeated runs pile up duplicate copies.
    snapshot = json.dumps({"name": seed["name"], "kind": seed["kind"],
                           "items": seed["items"]}, indent=2, ensure_ascii=False)
    if not any(p.read_text(encoding="utf-8") == snapshot
               for p in SEEDS_DIR.glob("*.json")):
        (SEEDS_DIR / f"{stamp}_{seed['kind']}.json").write_text(
            snapshot, encoding="utf-8")

    print(f"seed: {seed['name']} ({seed['kind']}, {len(seed['items'])} items)")

    if not args.provider:
        args.provider = ["ollama:llama3.1:8b"]

    for provider in args.provider:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", provider).strip("-")
        run_id = f"{stamp}_{slug}" + (f"_{args.label}" if args.label else "")
        print(f"\n>>> {provider}")
        try:
            if provider.startswith("ollama:"):
                result = run_ollama(seed, args, provider.split(":", 1)[1])
            elif provider in ("claude-sonnet", "claude-haiku"):
                result = run_anthropic(seed, args, provider)
            elif provider == "algorithmic":
                result = run_algorithmic(seed, args)
            elif provider == "claude-code":
                path = run_claude_code(seed, args, run_id)
                print(f"    prompt written -> {path}")
                print(f"    answer with a JSON array at -> "
                      f"{RESPONSES_DIR / (run_id + '.json')}")
                print(f"    then: python tools/playlist_bench.py --ingest {path} "
                      f"--response {RESPONSES_DIR / (run_id + '.json')}")
                continue
            else:
                print(f"    unknown provider: {provider}")
                continue
        except Exception as e:
            print(f"    FAILED: {type(e).__name__}: {e}")
            continue

        out = save_run(run_id, provider, seed, args, result)
        m = score_playlist(result["songs"],
                           seed["items"] if seed["kind"] != "discogs" else [])
        print(f"    {m.get('count', 0)} songs in {result.get('seconds')}s "
              f"— {m.get('unique_artists', 0)} artists, "
              f"{m.get('unique_decades', 0)} decades")
        print(f"    saved -> {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
