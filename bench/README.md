# bench/

Evidence for how the different playlist-generation approaches actually behave.
Produced by [`tools/playlist_bench.py`](../tools/playlist_bench.py) and
summarised by `compare_runs.py` and `verify_runs.py`.

| Path | What's in it |
|---|---|
| `seeds/` | Input playlists. `demo_playlist.txt` is a 25-track seed used for the runs below; drop your own `Artist - Title` files here. |
| `prompts/` | Prompts emitted for the `claude-code` provider (the no-API-key path), as JSON plus a readable `.txt`. |
| `responses/` | Answers written back for those prompts, before ingestion. |
| `runs/` | One saved run per provider — full song list, metrics, timing — as JSON and Markdown. |
| `runs/pre-fix/` | Runs from before the Ollama fixes, kept as the "before" evidence. |
| `comparison.md` | Metrics table, provider overlap, unique picks. |
| `verification.md` | Whether the recommended songs exist at all. |

## Headline result

Same seed, same settings (25 songs, discovery 40, `similar_songs`):

| Provider | Songs | Time | Unique artists | Picks that resolve to a real recording |
|---|---|---|---|---|
| `claude-code` | 25 | — | 25 | **100%** |
| `ollama:qwen3:30b-a3b` | 24 | 30 s | 19 | 79% |
| `ollama:llama3.1:8b` | 25 | 20 s | 25 | 48% |
| `ollama:gemma3:27b` | 25 | 64 s | 23 | 44% |

Two things this table hides, both in the detail reports:

- `qwen3`'s 79% is flattered by **what** it picks. Two thirds of its
  recommendations are by artists already in the seed — safe, well-indexed, and
  not really recommendations. `compare_runs.py` reports this as `Seed reuse`.
- `gemma3` has the highest self-reported obscurity and genuinely turns up good
  deep cuts (This Mortal Coil, Delia Derbyshire, Julee Cruise) — but it invents
  titles for real artists at roughly the same rate.

The `pre-fix/` runs show the state before schema-constrained decoding:
`qwen3:30b-a3b` returned 5 songs in 72 s with three of four batches empty.
