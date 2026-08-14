# Playlist generation — provider comparison

Seed: **demo_playlist** (tracks, 25 items)

## Metrics

| Provider | Songs | Time (s) | Artists | Artist div. | Decades | No year | Seed reuse | Seed leak | Dupes | Fields OK | Obscurity | Reason len |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `claude-code` | 25 | — | 25 | 1.0 | 4 | 0 | 0.0 | 0 | 0 | 1.0 | 58.0 | 163.6 |
| `ollama:gemma3:27b` | 25 | 63.8 | 23 | 0.92 | 6 | 0 | 0.0 | 0 | 0 | 1.0 | 67.6 | 146.2 |
| `ollama:llama3.1:8b` | 25 | 20.3 | 25 | 1.0 | 6 | 0 | 0.0 | 0 | 0 | 1.0 | 40.8 | 108.3 |
| `ollama:qwen3:30b-a3b` | 24 | 29.5 | 19 | 0.792 | 6 | 0 | 0.667 | 0 | 0 | 1.0 | 48.6 | 134.3 |

**Reading the table.** `Artist div.` is unique artists / songs (1.0 = no artist repeats). `Seed reuse` is the share of picks by an artist already in the seed. `Seed leak` counts songs copied straight from the seed — should always be 0. `Fields OK` is the share of songs with artist, title and year all present.

## Overlap

Shared song picks between providers (song-level, case-insensitive).

| | `claude-code` | `ollama:gemma3:27b` | `ollama:llama3.1:8b` | `ollama:qwen3:30b-a3b` |
|---|---|---|---|---|
| `claude-code` | — | 1 | 0 | 0 |
| `ollama:gemma3:27b` | 1 | — | 1 | 1 |
| `ollama:llama3.1:8b` | 0 | 1 | — | 1 |
| `ollama:qwen3:30b-a3b` | 0 | 1 | 1 | — |

Artist-level overlap:

| | `claude-code` | `ollama:gemma3:27b` | `ollama:llama3.1:8b` | `ollama:qwen3:30b-a3b` |
|---|---|---|---|---|
| `claude-code` | — | 3 | 1 | 0 |
| `ollama:gemma3:27b` | 3 | — | 2 | 2 |
| `ollama:llama3.1:8b` | 1 | 2 | — | 3 |
| `ollama:qwen3:30b-a3b` | 0 | 2 | 3 | — |

## Picks only one provider made


### `claude-code` — 21 unique artists

- **Faust — Jennifer** (1973)
- **Manuel Göttsching — Sunrain** (1976)
- **The High Llamas — Checking In, Checking Out** (1996)
- **Laika — Coming Down Glass** (1994)
- **Beth Gibbons & Rustin Man — Mysteries** (2002)
- **Tricky — Aftermath** (1995)
- **Bark Psychosis — Big Shot** (1994)
- **Disco Inferno — Second Language** (1994)
- **Seefeel — Plainsong** (1993)
- **Autechre — Eutow** (1995)
- **Flying Saucer Attack — My Dreaming Hill** (1995)
- **Stars of the Lid — Requiem for Dying Mothers, Pt. 1** (2001)
- **Laraaji — The Dance No. 1** (1980)
- **Magazine — The Light Pours Out of Me** (1978)
- **Felt — Primitive Painters** (1985)
- _…and 6 more_

### `ollama:gemma3:27b` — 19 unique artists

- **Amon Tobin — Slow Day** (2007)
- **Popol Vuh — Aguas Electricas** (1975)
- **This Mortal Coil — Song to the Siren** (1983)
- **Spring Heel Jack — Every Day I Feel Safer** (1997)
- **Duncan Browne — Wilder Than Most People** (1972)
- **Loren Mazé & Lee Fraser — Sun Nebula** (2016)
- **Michael Rother — Fernlicht** (1979)
- **Delia Derbyshire — Zinzolin** (1968)
- **Popol Vuh — Aguas Eternas** (1975)
- **The Velvet Underground — Venus in Furs** (1967)
- **Brian Gascoigne — Echos of the Machine Age** (1986)
- **Duncan Browne — Journey Home** (1972)
- **Julee Cruise — Falling** (1989)
- **Penguin Cafe Orchestra — Peru** (1976)
- **Global Communication — 99999** (1994)
- _…and 4 more_

### `ollama:llama3.1:8b` — 20 unique artists

- **The Young Gods — Envoye** (1991)
- **The Haxan Cloak — Excavation** (2011)
- **Mira — Cinderella Man** (1995)
- **Fennesz — Endless Summer** (2001)
- **The Olivia Tremor Control — Jumping Fences (Penthouse Lamps)** (1996)
- **William Basinski — The Disintegration Loops, Pt. 1** (2002)
- **Kaito — Nebula** (2003)
- **La Düsseldorf — Lilac Haze** (1976)
- **Tujiko Norne — The Red Tree (A Song for the Sun)** (2007)
- **Galaxie 2000 — Camera Obsolescence II** (1981)
- **Boredoms — Super Roots 9** (1998)
- **Harold Budd & Robin Guthrie — The Pearl** (2006)
- **Sun Ra Arkestra — Discipline 27 2/3** (1964)
- **The Residents — The Third Reich 'n Roll** (1976)
- **Os Mutantes — Baby** (1968)
- _…and 5 more_

### `ollama:qwen3:30b-a3b` — 20 unique artists

- **Can — Spoon** (1972)
- **Broadcast — A Song from Under the Floorboards** (1997)
- **Talk Talk — It's My Life** (1984)
- **Stereolab — Ping Pong** (1992)
- **Cocteau Twins — Peppermint Pig** (1985)
- **Portishead — Roads** (1997)
- **My Bloody Valentine — You Made Me Realise** (1988)
- **Aphex Twin — Avril 14th** (1997)
- **Slowdive — Catch Me Now I'm Falling** (1991)
- **Sonic Youth — Teen Age Riot** (1988)
- **The Knife — Heartbeats** (2003)
- **Sigur Rós — Glósóli** (2002)
- **Björk — Hyperballad** (1995)
- **Grouper — The Light** (2014)
- **Neu! — Negativland** (1972)
- _…and 5 more_

## Picked by every provider (0 artists)

_None — the providers agreed on nothing._
