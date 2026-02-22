"""Shared fixtures for all test modules."""
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Set environment variables BEFORE importing anything that reads config
os.environ.setdefault("DISCOGS_TOKEN", "test_token_1234567890")
os.environ.setdefault("DISCOGS_USERNAME", "testuser")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-for-unit-tests")


@pytest.fixture
def sample_collection():
    """A realistic mini collection for testing."""
    return [
        {
            "id": 1001,
            "title": "OK Computer",
            "year": 1997,
            "artists": ["Radiohead"],
            "genres": ["Electronic", "Rock"],
            "styles": ["Alternative Rock", "Art Rock"],
            "labels": ["Parlophone"],
            "formats": ["Vinyl"],
            "thumb": "https://img.discogs.com/thumb1.jpg",
            "cover_image": "https://img.discogs.com/cover1.jpg",
            "url": "https://www.discogs.com/release/1001",
            "date_added": "2024-01-15T10:00:00",
        },
        {
            "id": 1002,
            "title": "Loveless",
            "year": 1991,
            "artists": ["My Bloody Valentine"],
            "genres": ["Rock"],
            "styles": ["Shoegaze"],
            "labels": ["Creation Records"],
            "formats": ["Vinyl"],
            "thumb": "https://img.discogs.com/thumb2.jpg",
            "cover_image": "https://img.discogs.com/cover2.jpg",
            "url": "https://www.discogs.com/release/1002",
            "date_added": "2024-02-01T10:00:00",
        },
        {
            "id": 1003,
            "title": "Dummy",
            "year": 1994,
            "artists": ["Portishead"],
            "genres": ["Electronic"],
            "styles": ["Trip Hop", "Downtempo"],
            "labels": ["Go! Beat"],
            "formats": ["CD"],
            "thumb": "https://img.discogs.com/thumb3.jpg",
            "cover_image": "https://img.discogs.com/cover3.jpg",
            "url": "https://www.discogs.com/release/1003",
            "date_added": "2024-03-01T10:00:00",
        },
        {
            "id": 1004,
            "title": "Kid A",
            "year": 2000,
            "artists": ["Radiohead"],
            "genres": ["Electronic", "Rock"],
            "styles": ["Art Rock", "Experimental"],
            "labels": ["Parlophone"],
            "formats": ["Vinyl"],
            "thumb": "",
            "cover_image": "",
            "url": "https://www.discogs.com/release/1004",
            "date_added": "2024-04-01T10:00:00",
        },
        {
            "id": 1005,
            "title": "Endtroducing.....",
            "year": 1996,
            "artists": ["DJ Shadow"],
            "genres": ["Electronic", "Hip Hop"],
            "styles": ["Trip Hop", "Abstract"],
            "labels": ["Mo Wax"],
            "formats": ["Vinyl"],
            "thumb": "",
            "cover_image": "",
            "url": "https://www.discogs.com/release/1005",
            "date_added": "2024-05-01T10:00:00",
        },
    ]


@pytest.fixture
def sample_profile():
    """Pre-built profile matching sample_collection."""
    return {
        "total_releases": 5,
        "top_genres": [("Electronic", 4), ("Rock", 3), ("Hip Hop", 1)],
        "top_styles": [
            ("Art Rock", 2), ("Trip Hop", 2), ("Alternative Rock", 1),
            ("Shoegaze", 1), ("Downtempo", 1), ("Experimental", 1), ("Abstract", 1),
        ],
        "top_artists": [("Radiohead", 2), ("My Bloody Valentine", 1),
                        ("Portishead", 1), ("DJ Shadow", 1)],
        "top_labels": [("Parlophone", 2), ("Creation Records", 1),
                       ("Go! Beat", 1), ("Mo Wax", 1)],
    }


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory for thumbs tests."""
    return tmp_path


@pytest.fixture
def sample_thumbs_data():
    """Sample thumbs data for testing."""
    return [
        {
            "artist": "Radiohead",
            "title": "Everything In Its Right Place",
            "album": "Kid A",
            "genres": ["Electronic", "Rock"],
            "styles": ["Art Rock"],
            "timestamp": "2024-06-01T10:00:00",
        },
        {
            "artist": "Portishead",
            "title": "Wandering Star",
            "album": "Dummy",
            "genres": ["Electronic"],
            "styles": ["Trip Hop"],
            "timestamp": "2024-06-02T10:00:00",
        },
    ]
