"""Tests for services/cache.py — covers CWE-20, CWE-400, CWE-770."""
import time
from unittest.mock import patch

import pytest

from services.cache import SimpleCache


class TestSimpleCacheBasic:
    """Basic get/set/invalidate operations."""

    def test_set_and_get(self):
        c = SimpleCache()
        c.set("key1", "value1")
        assert c.get("key1") == "value1"

    def test_get_missing_key(self):
        c = SimpleCache()
        assert c.get("nonexistent") is None

    def test_overwrite_key(self):
        c = SimpleCache()
        c.set("key1", "old")
        c.set("key1", "new")
        assert c.get("key1") == "new"

    def test_invalidate(self):
        c = SimpleCache()
        c.set("key1", "value1")
        c.invalidate("key1")
        assert c.get("key1") is None

    def test_invalidate_missing_key_noop(self):
        c = SimpleCache()
        c.invalidate("nonexistent")  # Should not raise

    def test_invalidate_prefix(self):
        c = SimpleCache()
        c.set("user:1:data", "a")
        c.set("user:2:data", "b")
        c.set("other:1", "c")
        c.invalidate_prefix("user:")
        assert c.get("user:1:data") is None
        assert c.get("user:2:data") is None
        assert c.get("other:1") == "c"

    def test_clear(self):
        c = SimpleCache()
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert c.size() == 0

    def test_size(self):
        c = SimpleCache()
        assert c.size() == 0
        c.set("a", 1)
        assert c.size() == 1
        c.set("b", 2)
        assert c.size() == 2

    def test_stores_various_types(self):
        c = SimpleCache()
        c.set("list", [1, 2, 3])
        c.set("dict", {"a": 1})
        c.set("int", 42)
        c.set("none", None)
        assert c.get("list") == [1, 2, 3]
        assert c.get("dict") == {"a": 1}
        assert c.get("int") == 42
        assert c.get("none") is None  # None is stored but indistinguishable from miss


class TestSimpleCacheTTL:
    """TTL expiration behavior."""

    def test_expired_entry_returns_none(self):
        c = SimpleCache()
        c.set("key", "value", ttl=0)
        # TTL=0 means expires at time.time() + 0 = now
        time.sleep(0.01)
        assert c.get("key") is None

    def test_custom_ttl(self):
        c = SimpleCache()
        c.set("key", "value", ttl=10)
        assert c.get("key") == "value"

    def test_default_ttl_used(self):
        c = SimpleCache(default_ttl=5)
        c.set("key", "value")
        assert c.get("key") == "value"

    def test_negative_ttl_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="TTL must be non-negative"):
            c.set("key", "value", ttl=-1)

    def test_expired_entries_cleaned_on_get(self):
        c = SimpleCache()
        c.set("key", "value", ttl=0)
        time.sleep(0.01)
        c.get("key")
        assert c.size() == 0


class TestSimpleCacheKeyValidation:
    """Key validation — CWE-20 (Improper Input Validation)."""

    def test_empty_key_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="non-empty string"):
            c.set("", "value")

    def test_empty_key_get_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="non-empty string"):
            c.get("")

    def test_non_string_key_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="non-empty string"):
            c.set(123, "value")  # type: ignore

    def test_long_key_raises(self):
        c = SimpleCache()
        long_key = "x" * 257
        with pytest.raises(ValueError, match="exceeds"):
            c.set(long_key, "value")

    def test_max_length_key_ok(self):
        c = SimpleCache()
        key = "x" * 256
        c.set(key, "value")
        assert c.get(key) == "value"

    def test_empty_prefix_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="non-empty string"):
            c.invalidate_prefix("")

    def test_non_string_prefix_raises(self):
        c = SimpleCache()
        with pytest.raises(ValueError, match="non-empty string"):
            c.invalidate_prefix(None)  # type: ignore


class TestSimpleCacheResourceLimits:
    """Resource limits — CWE-400 (Uncontrolled Resource Consumption), CWE-770."""

    def test_max_entries_eviction(self):
        c = SimpleCache(max_entries=3)
        c.set("a", 1, ttl=3600)
        c.set("b", 2, ttl=3600)
        c.set("c", 3, ttl=3600)
        # Adding a 4th should evict the oldest
        c.set("d", 4, ttl=3600)
        assert c.size() == 3
        assert c.get("d") == 4

    def test_expired_entries_evicted_before_oldest(self):
        c = SimpleCache(max_entries=2)
        c.set("old", "val", ttl=0)
        time.sleep(0.01)
        c.set("new1", "val1", ttl=3600)
        # This should evict expired "old", not "new1"
        c.set("new2", "val2", ttl=3600)
        assert c.size() == 2
        assert c.get("new1") == "val1"
        assert c.get("new2") == "val2"

    def test_max_entries_one(self):
        c = SimpleCache(max_entries=1)
        c.set("a", 1)
        c.set("b", 2)
        assert c.size() == 1
        assert c.get("b") == 2

    def test_default_ttl_minimum_one(self):
        c = SimpleCache(default_ttl=0)
        assert c.default_ttl == 1

    def test_max_entries_minimum_one(self):
        c = SimpleCache(max_entries=0)
        assert c.max_entries == 1
