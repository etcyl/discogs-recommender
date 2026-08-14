# Playlist generation — provider comparison

Seed: **Liked Songs Copy** (spotify, 100 items)

## Metrics

| Provider | Songs | Time (s) | Artists | Artist div. | Decades | No year | Seed reuse | Seed leak | Dupes | Fields OK | Obscurity | Reason len |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `claude-code` | 25 | — | 25 | 1.0 | 3 | 0 | 0.0 | 0 | 0 | 1.0 | 43.3 | 153.0 |
| `ollama:gemma3:27b` | 25 | 65.7 | 21 | 0.84 | 5 | 0 | 0.04 | 0 | 0 | 1.0 | 41.2 | 146.3 |
| `ollama:llama3.1:8b` | 25 | 22.5 | 25 | 1.0 | 6 | 0 | 0.0 | 0 | 0 | 1.0 | 34.0 | 101.1 |
| `ollama:qwen3:30b-a3b` | 25 | 21.9 | 23 | 0.92 | 3 | 0 | 0.2 | 0 | 0 | 1.0 | 32.1 | 135.0 |

**Reading the table.** `Artist div.` is unique artists / songs (1.0 = no artist repeats). `Seed reuse` is the share of picks by an artist already in the seed. `Seed leak` counts songs copied straight from the seed — should always be 0. `Fields OK` is the share of songs with artist, title and year all present.

## Overlap

Shared song picks between providers (song-level, case-insensitive).

| | `claude-code` | `ollama:gemma3:27b` | `ollama:llama3.1:8b` | `ollama:qwen3:30b-a3b` |
|---|---|---|---|---|
| `claude-code` | — | 0 | 0 | 0 |
| `ollama:gemma3:27b` | 0 | — | 1 | 0 |
| `ollama:llama3.1:8b` | 0 | 1 | — | 0 |
| `ollama:qwen3:30b-a3b` | 0 | 0 | 0 | — |

Artist-level overlap:

| | `claude-code` | `ollama:gemma3:27b` | `ollama:llama3.1:8b` | `ollama:qwen3:30b-a3b` |
|---|---|---|---|---|
| `claude-code` | — | 1 | 0 | 1 |
| `ollama:gemma3:27b` | 1 | — | 4 | 3 |
| `ollama:llama3.1:8b` | 0 | 4 | — | 1 |
| `ollama:qwen3:30b-a3b` | 1 | 3 | 1 | — |

## Picks only one provider made


### `claude-code` — 23 unique artists

- **Jimpster — Dangly Panther** (2006)
- **Isolée — Beau Mot Plage** (1998)
- **Kerri Chandler — Rain** (2007)
- **Âme — Rej** (2005)
- **Motor City Drum Ensemble — Raw Cuts #3** (2009)
- **Nosaj Thing — Aquarium** (2009)
- **TOKiMONSTA — Lucid Waking** (2010)
- **Machinedrum — Gunshotta** (2013)
- **Clams Casino — I'm God** (2011)
- **Balam Acab — See Birds (Moon)** (2011)
- **Com Truise — Brokendate** (2011)
- **Salvia Palth — i was all over her** (2013)
- **Glassjaw — Tip Your Bartender** (2002)
- **Quicksand — Fazer** (1993)
- **Far — Mother Mary** (1998)
- _…and 8 more_

### `ollama:gemma3:27b` — 14 unique artists

- **DJ Koze — Picknick** (2013)
- **Boards of Canada — Roygbiv** (1998)
- **J Dilla — So Far To Go (feat. Common & D'Angelo)** (2006)
- **Portishead — Glory Box** (1994)
- **Madlib — Mystikal Message** (2003)
- **DJ Shadow — Midnight in a Perfect World** (1996)
- **Brian Eno — An Ending (Ascent)** (1983)
- **Massive Attack — Teardrop** (1998)
- **J Dilla — Donuts (Outro)** (2006)
- **Gang Starr — Mass Appeal** (1994)
- **Gotan Project — Santa Maria (Del Buen Ayre)** (2005)
- **Amón Tobin — Slow Day** (2007)
- **Sven Väth — L.I.E.B.** (1998)
- **BADBADNOTGOOD — Time Moves Slow (feat. Samuel T. Herring)** (2016)

### `ollama:llama3.1:8b` — 20 unique artists

- **Nujabes — Aruarian Dance** (2005)
- **Sango — Mama** (2015)
- **The Heliocentrics — Sax 'N' Sitar** (2012)
- **Sun Ra — Space is the Place** (1972)
- **Mild High Club — Windowpane** (2016)
- **Kraftwerk — The Hall of Mirrors** (1977)
- **Daniel Caesar — Get You** (2017)
- **Boris — No** (2003)
- **Yotam Silberman — Woven Heart** (2019)
- **Yma Sumac — Donde Estabas Tu?** (1959)
- **Tenderlonious — Bodhi Tree** (2017)
- **La Monte Young — Composition for Two Elephants** (1961)
- **The Residents — Constantinople** (1978)
- **The Velvet Underground — Sister Ray** (1968)
- **Bert Jansch — Black Water Side** (1966)
- _…and 5 more_

### `ollama:qwen3:30b-a3b` — 20 unique artists

- **Lone — Canyon** (2012)
- **Tune-Yards — Bizness** (2010)
- **The Avalanches — Since I Left You** (2000)
- **Cibo Matto — Sack Lunch** (1995)
- **Battles — Atlas** (2007)
- **M83 — Midnight City** (2011)
- **Björk — Hyperballad** (1995)
- **Tame Impala — Elephant** (2012)
- **Mogwai — Mogwai Fear Satan** (1997)
- **Porter Robinson — Look at the Sky** (2016)
- **The Knife — We Share Our Mothers' Health** (2003)
- **Nils Frahm — Says** (2012)
- **Sufjan Stevens — Fourth of July** (2005)
- **FKA twigs — Two Weeks** (2014)
- **Kendrick Lamar — mortal man** (2015)
- _…and 5 more_

## Picked by every provider (0 artists)

_None — the providers agreed on nothing._
