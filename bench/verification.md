# Do the recommended songs actually exist?

Every pick resolved against Deezer, then iTunes, then MusicBrainz. A track is **verified** when a catalogue returns a close artist+title match. **Unverified** means no catalogue had it — usually invented, occasionally just too obscure to be indexed.

## Summary

| Provider | Tracks | Verified | Unverified | Verified % |
|---|---|---|---|---|
| `claude-code` | 25 | 25 | 0 | **100.0%** |
| `ollama:qwen3:30b-a3b` | 24 | 19 | 5 | **79.2%** |
| `ollama:llama3.1:8b` | 25 | 12 | 13 | **48.0%** |
| `ollama:gemma3:27b` | 25 | 11 | 14 | **44.0%** |

## Spot-check — is "unverified" really "invented"?

A catalogue miss could mean the track is genuinely too obscure to be indexed,
so the misses were checked by hand. Overwhelmingly they are real errors, and
they cluster into recognisable failure modes:

| Pick | What it actually is |
|---|---|
| `Portishead — Silent Shout` | a **The Knife** song |
| `Slowdive — Catch Me Now I'm Falling` | a **Kinks** song |
| `Broadcast — A Song from Under the Floorboards` | a **Magazine** song |
| `Cluster — By This River` | a **Brian Eno** song (co-written *with* Cluster) |
| `Harold Budd & Robin Guthrie — The Pearl` | *The Pearl* is Budd & **Eno**, 1984 |
| `Nico — Janitor for God` | the song is **Janitor of Lunacy** |
| `Galaxie 2000 — …` | the band is **Galaxie 500** |
| `Tujiko Norne — …` | the artist is **Tujiko Noriko** |
| `Penguin Cafe Orchestra — Peru` | no such track |
| `Delia Derbyshire — Zinzolin` | no such track |
| `Global Communication — 99999` | their tracks are timecodes ("14:31") |

So: **real artist, wrong song** (the song belongs to someone else), **real
pairing, wrong record**, and **near-miss artist names**. All three are worse
than an outright refusal, because they look authoritative.

Two are arguably false negatives rather than inventions — `Sun Ra Arkestra —
Discipline 27 2/3` is a real work (*Discipline 27-II*, 1973) with a mangled
title and a wrong year, and `William Basinski — The Disintegration Loops,
Pt. 1` names the album rather than the track (`dlp 1.1`). Correcting for those
moves `llama3.1:8b` from 48% to about 56% — it does not change the picture.

## Unverified picks


### `claude-code` — 0 unverified of 25

_Every pick resolved to a real recording._

### `ollama:gemma3:27b` — 14 unverified of 25

- Amon Tobin — Slow Day (2007)
- Cluster — By This River (1974)
- Popol Vuh — Aguas Electricas (1975)
- Spring Heel Jack — Every Day I Feel Safer (1997)
- Duncan Browne — Wilder Than Most People (1972)
- Loren Mazé & Lee Fraser — Sun Nebula (2016)
- Michael Rother — Fernlicht (1979)
- Delia Derbyshire — Zinzolin (1968)
- Popol Vuh — Aguas Eternas (1975)
- Brian Gascoigne — Echos of the Machine Age (1986)
- Duncan Browne — Journey Home (1972)
- Penguin Cafe Orchestra — Peru (1976)
- Global Communication — 99999 (1994)
- The Feelies — Faustian Deception (1980)

### `ollama:llama3.1:8b` — 13 unverified of 25

- Mira — Cinderella Man (1995)
- The Olivia Tremor Control — Jumping Fences (Penthouse Lamps) (1996)
- William Basinski — The Disintegration Loops, Pt. 1 (2002)
- La Düsseldorf — Lilac Haze (1976)
- Tujiko Norne — The Red Tree (A Song for the Sun) (2007)
- Galaxie 2000 — Camera Obsolescence II (1981)
- The Advisory Circle — Heritage (2005)
- Harold Budd & Robin Guthrie — The Pearl (2006)
- Sun Ra Arkestra — Discipline 27 2/3 (1964)
- The Heliocentrics — Aurora (2008)
- Heldon — Electronic Happening No. 2 (1976)
- Einstürzende Neubauten — Alles hat ein Ende (1981)
- Nico — Janitor for God (1974)

### `ollama:qwen3:30b-a3b` — 5 unverified of 24

- Broadcast — A Song from Under the Floorboards (1997)
- Slowdive — Catch Me Now I'm Falling (1991)
- Grouper — The Light (2014)
- Broadcast — The Only Melody (1999)
- Portishead — Silent Shout (2004)
