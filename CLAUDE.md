# Discogs Recommender — Claude Instructions

## Versioning (MANDATORY)

This project uses **semantic versioning** tracked in the `VERSION` file at the repo root.
The version is displayed in the app footer via `APP_VERSION` in `app.py`.

### Format: `MAJOR.MINOR.PATCH`

- **MAJOR** (X.0.0) — Breaking changes, major redesigns, fundamental architecture shifts
- **MINOR** (0.X.0) — New features: new pages, new channel types, new integrations, new service files
- **PATCH** (0.0.X) — Bug fixes, tweaks, small improvements, CSS changes, wording changes, refactors

### Rules for every session

1. **Before finishing work**, bump the version in the `VERSION` file:
   - Bug fix or small tweak → bump PATCH (e.g. `1.5.0` → `1.5.1`)
   - New feature or significant enhancement → bump MINOR, reset PATCH (e.g. `1.5.1` → `1.6.0`)
   - Breaking/major change → bump MAJOR, reset MINOR and PATCH (e.g. `1.6.0` → `2.0.0`)
2. **When in doubt**, bump PATCH. Over-bumping MINOR is fine; under-bumping is not.
3. **Multiple changes in one session**: use the highest-impact bump. Don't bump multiple times within a single session.
4. **Read `VERSION` first** to know the current version before bumping.

### Where version lives
- `VERSION` — single-line file, the source of truth (e.g. `1.5.0`)
- `app.py` — reads `VERSION` at startup into `APP_VERSION`, injected into all templates
- `templates/base.html` — displays `v{{ app_version }}` in the footer
