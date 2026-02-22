import time
from typing import Any, Optional


class SimpleCache:
    """In-memory cache with TTL expiration and max-entry eviction."""

    MAX_KEY_LENGTH = 256
    MAX_ENTRIES = 1000  # default; overridable at init

    def __init__(self, default_ttl: int = 3600, max_entries: int = 1000):
        self._store: dict[str, tuple[float, Any]] = {}
        self.default_ttl = max(1, default_ttl)
        self.max_entries = max(1, max_entries)

    def _validate_key(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("Cache key must be a non-empty string")
        if len(key) > self.MAX_KEY_LENGTH:
            raise ValueError(f"Cache key exceeds {self.MAX_KEY_LENGTH} characters")

    def _evict_expired(self) -> None:
        """Remove all expired entries."""
        now = time.time()
        expired = [k for k, (expiry, _) in self._store.items() if now >= expiry]
        for k in expired:
            del self._store[k]

    def get(self, key: str) -> Optional[Any]:
        self._validate_key(key)
        if key in self._store:
            expiry, value = self._store[key]
            if time.time() < expiry:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: Any, ttl: int = None) -> None:
        self._validate_key(key)
        ttl = ttl if ttl is not None else self.default_ttl
        if ttl < 0:
            raise ValueError("TTL must be non-negative")

        # Evict expired entries before checking size
        if len(self._store) >= self.max_entries:
            self._evict_expired()

        # If still at capacity, evict oldest entry
        if len(self._store) >= self.max_entries:
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]

        self._store[key] = (time.time() + ttl, value)

    def invalidate(self, key: str) -> None:
        self._validate_key(key)
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        if not isinstance(prefix, str) or not prefix:
            raise ValueError("Prefix must be a non-empty string")
        keys_to_delete = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._store[k]

    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()


cache = SimpleCache()
