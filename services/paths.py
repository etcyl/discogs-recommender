"""Where the app keeps its data.

One place, so the location can be redirected in one step. That matters for
tests: importing `app` bootstraps an admin user and writes it to disk, so
without a redirect a test run writes its fixture credentials straight into the
developer's real database — which then gets used by the running app.

Set DISCOGS_DATA_DIR to move everything (database, per-user JSON, caches).
"""
import os
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent.parent / "data"


def data_dir() -> Path:
    """The root data directory, resolved fresh each call.

    Deliberately not cached at import time: tests set the environment variable
    before importing the app, but the module may already have been imported by
    something else in the same process.
    """
    override = os.environ.get("DISCOGS_DATA_DIR")
    return Path(override) if override else _DEFAULT


# Convenience for module-level constants that predate this helper.
DATA_DIR = data_dir()
