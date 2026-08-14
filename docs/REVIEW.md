# Code review — architecture, bugs, and where to optimise

Written after cloning the repo fresh, standing it up locally on Windows,
running the test suite, running the app, and driving the playlist pipeline
end-to-end against three local models.

- [How it works](#how-it-works)
- [Bugs found and fixed](#bugs-found-and-fixed)
- [Where the remaining wins are](#where-the-remaining-wins-are)
- [UI](#ui)

---

## How it works

```
Browser
  │  HTML pages (Jinja2) + vanilla JS
  │  SSE for playlist generation
  ▼
app.py ── FastAPI, ~2,400 lines, ~50 routes
  │      middleware: TrustedHost → security headers → auth (session cookie)
  │
  ├── services/
  │   ├── discogs_service.py       Discogs REST wrapper, 429 backoff
  │   ├── recommendation.py        CollectionAnalyzer: profile + candidate scoring
  │   ├── radio_service.py         prompt construction, batching, YouTube resolution
  │   ├── llm_provider.py          Claude API / Ollama dispatch + JSON recovery
  │   ├── channel_service.py       channel CRUD (JSON file per user)
  │   ├── thumbs.py                likes / dislikes / play history (JSON file per user)
  │   ├── credit_service.py        producer + session-musician graph from Discogs
  │   ├── scene_service.py         scene clustering, label family trees
  │   ├── preference_service.py    learned attribute weights from feedback
  │   ├── spotify_service.py       public playlist scrape (no credentials)
  │   ├── youtube_playlist_service.py  yt-dlp flat extraction (no API key)
  │   ├── auth_service.py          users, sessions, invites
  │   ├── database.py              SQLite schema + migrations
  │   └── cache.py                 in-process TTL cache
  │
  └── data/                        SQLite (users) + per-user JSON (everything else)
```

### The playlist pipeline

This is the heart of the app. A request to
`GET /api/radio/playlist-stream?channel_id=…` runs:

1. **Cache probe.** `radio_playlist:{user}:{channel}`, 8h TTL. On a hit, songs
   the listener has since disliked or heard are filtered out and the cached
   list is streamed immediately.
2. **Branch on `source_type`** — `discogs`, `spotify`, `youtube`, `upload`, or
   `liked`. Each branch assembles its own seed material.
3. **Build the seed context.** For a Discogs channel that means the collection
   profile (top genres/styles/labels/artists), plus optional enrichment:
   the credit graph, scene clusters, the label family tree, and learned
   attribute preferences. Enrichment is Sonnet-only by default.
4. **Generate.** `RadioService._batched_generate` fires 2–3 LLM calls in
   parallel, each with a different "variety hint" so the batches don't
   converge on the same obvious picks, then deduplicates across batches
   (exact key *and* a normalised key), applies the era filter, and tops up
   with a follow-up call if it came up short.
5. **Sequence.** `_spread_artists` greedily reorders so consecutive tracks
   differ in artist and decade.
6. **Resolve to YouTube.** An 8-thread pool searches YouTube per song, scores
   each candidate on title-word and artist-word overlap, rejects weak matches,
   rewrites artist/title from the YouTube title, then enriches album art and
   year from iTunes/Deezer.
7. **Stream** results to the browser in chunks as they resolve, and cache.

The design decision worth calling out: **the YouTube title is treated as the
source of truth** and overwrites what the model said. That is a good instinct
— it corrects model typos — but it also means a mis-resolved video silently
rewrites a correct recommendation into a wrong one. The post-resolution
filter that re-checks the exclude set exists precisely because of this.

---

## Bugs found and fixed

Everything in this section is fixed in the working tree, with the test suite
green (339 passing).

### 1. A fresh install could not render a single page

`requirements.txt` pins nothing (`fastapi>=0.110.0`), so a clean install today
pulls Starlette 1.6, which **removed** the legacy
`TemplateResponse(name, {"request": request, …})` signature. All 18 call sites
used it. Every HTML route raised `TypeError: unhashable type: 'dict'` — the
dict was being taken as the template *name* and used as a cache key.

26 of the 31 test failures on a fresh clone came from this one line shape.
Fixed by migrating to `TemplateResponse(request, name, context)`.

> Worth pinning upper bounds in `requirements.txt` so this cannot recur.

### 2. Bracketed song titles silently destroyed whole LLM batches

`parse_llm_json` recovered truncated output by taking `text.rfind("]")`. That
scan is not string-aware, so a title like `Blue Monday [12" Mix]` put the last
`]` *inside a string value*. The fragment was unparseable, the `}` fallback
then also failed, and the entire batch — up to 25 songs — was discarded.

Replaced with a depth-aware, string-aware scanner that extracts every complete
top-level object and ignores whatever was cut off. Bracketed and quoted titles
are common enough in real catalogues that this fired regularly.

### 3. Reasoning models produced literally nothing

`qwen3:30b-a3b` and its relatives run a hidden reasoning pass whose tokens are
billed against `max_tokens` but never appear in `content`. With the playlist
prompt it reasoned until the budget was gone and returned an empty string —
every time.

Measured on the demo seed, 25 songs requested:

| Attempt | Result |
|---|---|
| As shipped | 5 songs, 71.6 s, 3 of 4 batches empty |
| `reasoning_effort: low` | 11 songs, 54.3 s, 2 batches empty |
| `+ 3000 tokens headroom` | 10 songs, 95.1 s, 3 batches empty |
| **Schema-constrained decoding** | **24 songs, 29.5 s, 0 batches empty** |

Raising the token budget does not help — the model simply reasons longer.
`think: false` does not help either; it moves the reasoning into `content`.

The fix is Ollama's structured-output mode: pass a JSON Schema as `format` on
the native `/api/chat` endpoint so decoding is constrained to valid JSON.
It fixed reasoning models *and* removed prose preambles from the other models.
Parse failures across all three models went from routine to zero.

`llm_provider` detects reasoning models from `/api/show` capabilities and
caches the answer, so this is automatic per model.

### 4. "Atomic" writes were not atomic

`thumbs.py`, `channel_service.py` and `credit_service.py` each carry a copy of
`_atomic_write_json`, documented as protecting against CWE-367. Each did:

```python
if filepath.exists():
    filepath.unlink()      # ← window where the file does not exist
os.rename(tmp_path, filepath)
```

The `unlink` was there because `os.rename` won't overwrite on Windows — but it
opens exactly the gap the function exists to close. `os.replace` is atomic on
both POSIX and Windows. Fixed in all three, plus an `fsync` before the swap.

### 5. Discogs rate limiting was wired to the wrong call

```python
results = self._rate_limited_call(self.client.search, **kwargs)   # lazy — no HTTP
results.per_page = per_page
try:
    page_data = results.page(page)     # ← the real request, unprotected
except Exception:
    return []                          # ← 429 swallowed, returns "no results"
```

`client.search()` only builds a lazy paginated list. The HTTP request happens
in `.page()`, which sat inside a bare `except` that turned rate-limit errors
into empty result sets. Since the genre engine issues dozens of searches per
request, hitting the 60/min ceiling degraded recommendations *silently*.

Moved the backoff wrapper onto `.page()` and made the failure log.

### 6. Raw exceptions were streamed to the browser

The SSE generator's catch-all did `yield _sse("error", {"message": str(e)})`,
bypassing `_sanitize_error` entirely — the redaction the rest of the app
applies. Users saw `401: Invalid consumer token. Please register an app before
making requests.`

Now routed through the sanitizer, which also maps known upstream failures onto
something actionable ("Discogs rejected the API token. Check DISCOGS_TOKEN in
your .env — generate a new one at discogs.com/settings/developers.").

### 7. Setup notices waited on a slow hardware probe

`checkSystemStatus()` awaited `/api/system/hardware` — which shells out to
detect the GPU and can take seconds — before rendering *any* notice, including
the ones already known from `/api/system/status`. The banners appeared several
seconds after paint and shoved the page down. Hardware warnings are now
folded in when they arrive rather than gating the rest.

### 8. Five stale tests

Four asserted YouTube-matching behaviour that predates the current title
scorer (their mock results would be correctly rejected today); one asserted
that an empty `discogs_username` raises, which stopped being true when Discogs
became optional. Updated to match current behaviour, and the two
`resolve_youtube_ids` tests no longer reach out to iTunes/Deezer for real.

---

## Where the remaining wins are

Ordered by value. None of these are done.

### Performance

**1. `CollectionAnalyzer.get_recommendations` issues its Discogs searches
sequentially.** At high discovery it searches every style, artist and label in
the profile — easily 40+ round trips, one after another. At ~0.5–1 s each that
is 20–40 s of wall clock for one page load, and it can breach the 60/min limit
partway through (which, before the fix above, silently returned nothing).

Fix: a thread pool of 5–8 workers behind a shared token bucket sized to the
60/min budget. This is the single biggest latency win in the app.

**2. `get_full_collection` pages sequentially too** — a 1,000-record collection
is 10 serial requests. Same fix, same rate budget.

**3. `_batched_generate` reads futures in submission order.** `for future in
futures: future.result()` blocks on batch 0 even if batch 2 finished first, so
the `on_batch` progress the user sees is not live. `as_completed` is a
two-line change.

**4. `SimpleCache.set` is O(n) per insert once full** — `min()` over the whole
store to find the oldest entry. An `OrderedDict` with `move_to_end`/`popitem`
makes it O(1). Only matters at the 1,000-entry cap, but it's free.

**5. `DiscogsService` client objects live in the same TTL cache as data.**
A burst of cache writes can evict a live client, silently rebuilding it. Keep
clients in their own dict keyed by user.

### Structure

**6. `radio_playlist_stream` is a single ~510-line function** with five
near-identical `source_type` branches that each repeat: fetch seed → build
excludes → call the generator → rerank → handle `LLMError`. Extracting a
small `PlaylistSource` per type would remove most of it, and would mean a fix
like the error-sanitisation one above lands in one place instead of four.

**7. `channel_service` has six copy-pasted `update_channel_*` functions**
differing only in which key they set. One `update_channel(channel_id,
**fields)` with a whitelist replaces all six.

**8. The channel markup exists twice** — in `radio.html` and again as a
template literal in `radio.js`. They have already drifted (the JS copy hard-
codes `selected` on the "All Eras" option). Render it in one place.

**9. Three copies of `_atomic_write_json`.** One `services/jsonstore.py`.

**10. Per-user state is a pile of JSON files** (channels, thumbs, history,
credits), each fully read and fully rewritten on every mutation. There is
already a SQLite database in the app with WAL enabled. Moving this state into
it removes the whole atomic-write problem class and makes concurrent tabs safe.

### Security

**11. Discogs tokens are stored in plaintext** in `users.discogs_token`.
Anyone with read access to `data/users.db` has every user's Discogs
credentials. `cryptography` is already in the dependency tree.

> Partly mitigated: a **public** collection now needs no token at all, so the
> common single-user setup can avoid storing one. See
> `DiscogsService.public_mode`.

**12. No CSRF protection** on the state-changing form posts (`/login`,
`/admin/*`). Session auth is cookie-based, so these are cross-site submittable.

**13. `_rate_limits` never drops empty keys** — the defaultdict accumulates one
list per distinct key forever. Slow leak; prune on read.

**14. CSP allows `script-src 'unsafe-inline'`** because templates carry inline
handlers. Nonces would let that come off.

### Recommendation quality

**15. ~~Verify picks before showing them.~~ Done** — see
[SAFETY.md](SAFETY.md). Recommendations are now resolved against Deezer,
iTunes and MusicBrainz before reaching the player, with an `off` / `flag` /
`strict` policy, badges in the UI, and an audit log at `/audit`. The
measurements that motivated it are in
[`bench/verification.md`](../bench/verification.md).

**16. Give large local models the full prompt.** The prompt branch was chosen
by `ai_model in ("ollama", "claude-haiku")`, so a 27B local model got the same
stripped-down prompt as a 3B one. Added a `prompt_tier` setting
(`auto` | `compact` | `rich`); set `PROMPT_TIER=rich` in `.env` for 24B+ models.

**17. Constrain the Claude path too.** Claude follows the format instructions
well, but tool-use with an input schema would make malformed output impossible
there as well, for the same reason it did for Ollama.

---

## UI

### What was wrong

- **Two full-width banners** injected after first paint, eating roughly a
  quarter of the viewport on every page and visibly shoving content down.
- **One media query in 2,348 lines of CSS.** Effectively no responsive design
  outside the radio layout.
- **No `color-scheme` declaration**, so native controls — the `<select>`s and
  range inputs in the channel sidebar — rendered in light chrome on a dark UI.
  The two sliders in a channel row had visibly different thumbs because only
  one was styled.
- **No design tokens**: 65 hex literals and 187 `rgba()` calls against 25 CSS
  variables, so a colour change means a find-and-replace.
- **A dark theme with light-theme overrides but no way to switch.**
- **No `:focus-visible` styling and no `prefers-reduced-motion` handling**, on
  a page with a canvas visualiser and several animations.
- **Every channel row rendered five controls inline** — discovery slider, era
  select, model select, size slider, deep-cuts checkbox — about 220 px of
  chrome per channel, so three channels filled the sidebar. The model select
  was narrow enough to clip its own label to `Ollama (free, loca`.
- **Pico loaded from a CDN**, so the app is unstyled offline and blocks first
  paint on a third-party host.
- **Raw upstream errors** shown to the user.

### What changed

`static/css/tokens.css` (new) carries a token layer — surfaces, text, lines,
status colours, spacing, radius, elevation, motion, type scale — plus the
platform fixes: `color-scheme`, a single `:focus-visible` treatment, a
`prefers-reduced-motion` block, unified range-input styling, and
`scrollbar-gutter: stable` so page-to-page navigation stops shifting sideways.

Pico is vendored to `static/css/vendor/pico.min.css`. The nav is a sticky bar
with a real active state (`aria-current`), a theme toggle, and a no-flash
inline theme bootstrap. Setup notices collapse into one compact bar that
expands on click. Per-channel settings moved into a `<details>` drawer, so a
channel row is one line again and the model select has room for its label.

Screenshots: [`docs/before/`](before/) and [`docs/after/`](after/).

### Still worth doing

- `static/js/radio.js` is 2,165 lines / 96 KB served unminified, with 147
  `getElementById` calls and no node caching.
- Pico's blue primary button clashes with the amber brand accent; the three
  "get started" cards use two different button weights for three equally
  important actions.
- Pico's `<article><header>` leaves a large gap between card title and body.
