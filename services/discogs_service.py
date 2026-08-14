import logging
import time

import discogs_client
from discogs_client.exceptions import HTTPError

logger = logging.getLogger(__name__)

MAX_SEARCH_FIELD_LENGTH = 200
MAX_PER_PAGE = 100


def _sanitize_search_input(value: str | None) -> str | None:
    """Sanitize search input: strip, truncate, remove control characters (CWE-20)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    # Remove null bytes and control characters
    cleaned = "".join(c for c in value if c.isprintable())
    cleaned = cleaned.strip()[:MAX_SEARCH_FIELD_LENGTH]
    return cleaned if cleaned else None


class PublicCollectionError(Exception):
    """Raised when a public collection can't be read (private, missing, blocked)."""


class DiscogsService:
    """Discogs access, with or without a token.

    With a token, everything works. Without one, the service runs in **public
    mode**: a Discogs collection that its owner has left public is readable
    over the plain REST API with nothing but a User-Agent, so collection
    features still work. Database search and full release detail do require a
    token and raise a clear error in public mode rather than failing obscurely.
    """

    API_BASE = "https://api.discogs.com"

    def __init__(self, app_name: str, user_token: str, username: str):
        self.app_name = app_name
        self.username = username
        self.user_token = user_token or ""
        self.public_mode = not bool(user_token)
        self.client = (None if self.public_mode
                       else discogs_client.Client(app_name, user_token=user_token))

    # -- public-mode plumbing ------------------------------------------------

    def _public_get(self, path: str, params: dict | None = None) -> dict:
        """GET against the public REST API with backoff. Public mode only."""
        import httpx  # local import: only public mode needs it

        url = f"{self.API_BASE}{path}"
        headers = {"User-Agent": self.app_name}
        last_error = None
        for attempt in range(3):
            try:
                resp = httpx.get(url, params=params, headers=headers, timeout=20.0)
            except Exception as e:
                last_error = e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code == 404:
                raise PublicCollectionError(
                    f"Discogs has no public collection for user '{self.username}'. "
                    "Check the username, or add a DISCOGS_TOKEN if the collection "
                    "is private.")
            if resp.status_code in (401, 403):
                raise PublicCollectionError(
                    f"'{self.username}' collection is not public. Add a "
                    "DISCOGS_TOKEN to your .env to read a private collection.")
            resp.raise_for_status()
            return resp.json()
        raise PublicCollectionError(
            f"Could not reach the Discogs API: {last_error or 'rate limited'}")

    def _public_collection_page(self, page: int, per_page: int) -> dict:
        return self._public_get(
            f"/users/{self.username}/collection/folders/0/releases",
            {"per_page": per_page, "page": page})

    @staticmethod
    def _serialize_public_item(item: dict) -> dict:
        """Same shape as _serialize_collection_item, from a raw API dict."""
        info = item.get("basic_information", {}) or {}
        return {
            "id": info.get("id"),
            "title": info.get("title", ""),
            "year": info.get("year"),
            "artists": [a.get("name", "") for a in info.get("artists", [])],
            "genres": info.get("genres", []) or [],
            "styles": info.get("styles", []) or [],
            "labels": [la.get("name", "") for la in info.get("labels", [])],
            "formats": [f.get("name", "") for f in info.get("formats", [])],
            "thumb": info.get("thumb", ""),
            "cover_image": info.get("cover_image", ""),
            "url": f"https://www.discogs.com/release/{info.get('id')}",
            "date_added": item.get("date_added", ""),
        }

    # -- collection ----------------------------------------------------------

    def get_collection_page(self, page: int = 1, per_page: int = 100) -> dict:
        """Fetch a single page of releases from folder 0 (All)."""
        per_page = min(max(1, per_page), MAX_PER_PAGE)
        page = max(1, page)

        if self.public_mode:
            data = self._public_collection_page(page, per_page)
            pagination = data.get("pagination", {}) or {}
            return {
                "items": [self._serialize_public_item(i)
                          for i in data.get("releases", [])],
                "page": page,
                "pages": pagination.get("pages", 1),
                "total": pagination.get("items", 0),
            }

        me = self.client.identity()
        folder = me.collection_folders[0]
        releases = folder.releases
        releases.per_page = per_page
        page_data = self._rate_limited_call(releases.page, page)
        return {
            "items": [self._serialize_collection_item(item) for item in page_data],
            "page": page,
            "pages": releases.pages,
            "total": releases.count,
        }

    def get_full_collection(self) -> list[dict]:
        """Fetch ALL releases from the collection, paginating automatically."""
        all_releases = []

        if self.public_mode:
            page, pages = 1, 1
            while page <= pages:
                data = self._public_collection_page(page, 100)
                pages = (data.get("pagination", {}) or {}).get("pages", 1)
                all_releases.extend(self._serialize_public_item(i)
                                    for i in data.get("releases", []))
                page += 1
                if page <= pages:
                    # Unauthenticated callers get 25 requests/minute, so pace it.
                    time.sleep(1.1)
            logger.info("Loaded %d releases from %s's public collection (no token)",
                        len(all_releases), self.username)
            return all_releases

        me = self.client.identity()
        folder = me.collection_folders[0]
        releases = folder.releases
        releases.per_page = 100

        total_pages = releases.pages
        for page_num in range(1, total_pages + 1):
            page_data = self._rate_limited_call(releases.page, page_num)
            for item in page_data:
                all_releases.append(self._serialize_collection_item(item))

        return all_releases

    def get_release_details(self, release_id: int) -> dict:
        """Fetch full details for a single release."""
        if not isinstance(release_id, int) or release_id <= 0:
            raise ValueError("release_id must be a positive integer")
        if self.public_mode:
            return self._serialize_public_release(
                self._public_get(f"/releases/{release_id}"))
        release = self._rate_limited_call(self.client.release, release_id)
        return self._serialize_release(release)

    @staticmethod
    def _serialize_public_release(d: dict) -> dict:
        return {
            "id": d.get("id"),
            "title": d.get("title", ""),
            "year": d.get("year"),
            "artists": [a.get("name", "") for a in d.get("artists", [])],
            "genres": d.get("genres", []) or [],
            "styles": d.get("styles", []) or [],
            "labels": [la.get("name", "") for la in d.get("labels", [])],
            "formats": [f.get("name", "") for f in (d.get("formats") or [])],
            "tracklist": [{"position": t.get("position", ""),
                           "title": t.get("title", ""),
                           "duration": t.get("duration", "")}
                          for t in (d.get("tracklist") or [])],
            "images": [i.get("uri", i.get("uri150", ""))
                       for i in (d.get("images") or [])],
            "thumb": d.get("thumb", ""),
            "country": d.get("country", ""),
            "notes": d.get("notes", ""),
            "num_for_sale": d.get("num_for_sale"),
            "lowest_price": d.get("lowest_price"),
            "url": f"https://www.discogs.com/release/{d.get('id')}",
        }

    def search(self, query: str = None, type: str = "release",
               artist: str = None, genre: str = None,
               style: str = None, label: str = None,
               page: int = 1, per_page: int = 50) -> list[dict]:
        """Search the Discogs database with sanitized inputs."""
        if self.public_mode:
            # Database search is the one thing Discogs requires auth for, so
            # the genre engine needs a token even when the collection doesn't.
            raise PublicCollectionError(
                "Searching the Discogs database needs a DISCOGS_TOKEN. Your "
                "collection loads without one, but genre/style recommendations "
                "search the catalogue. Get a free token at "
                "discogs.com/settings/developers.")

        # Sanitize all inputs (CWE-20)
        query = _sanitize_search_input(query)
        artist = _sanitize_search_input(artist)
        genre = _sanitize_search_input(genre)
        style = _sanitize_search_input(style)
        label = _sanitize_search_input(label)
        per_page = min(max(1, per_page), MAX_PER_PAGE)
        page = max(1, page)

        # Validate search type
        allowed_types = {"release", "master", "artist", "label"}
        if type not in allowed_types:
            type = "release"

        kwargs = {"type": type}
        if query:
            kwargs["q"] = query
        if artist:
            kwargs["artist"] = artist
        if genre:
            kwargs["genre"] = genre
        if style:
            kwargs["style"] = style
        if label:
            kwargs["label"] = label

        # client.search() only builds a lazy paginated list — the HTTP request
        # happens on .page(), so that is the call that needs 429 backoff.
        results = self.client.search(**kwargs)
        results.per_page = per_page
        try:
            page_data = self._rate_limited_call(results.page, page)
        except HTTPError as e:
            logger.warning("Discogs search failed (HTTP %s) for %s", e.status_code, kwargs)
            return []
        except Exception as e:
            logger.warning("Discogs search failed for %s: %s", kwargs, e)
            return []

        serialized = []
        for item in page_data:
            serialized.append(self._serialize_search_result(item))
        return serialized

    def _serialize_collection_item(self, item) -> dict:
        """Serialize from collection item's basic_information (no extra API call)."""
        info = item.data.get("basic_information", {})
        return {
            "id": info.get("id"),
            "title": info.get("title", ""),
            "year": info.get("year"),
            "artists": [a["name"] for a in info.get("artists", [])],
            "genres": info.get("genres", []),
            "styles": info.get("styles", []),
            "labels": [la["name"] for la in info.get("labels", [])],
            "formats": [f["name"] for f in info.get("formats", [])],
            "thumb": info.get("thumb", ""),
            "cover_image": info.get("cover_image", ""),
            "url": f"https://www.discogs.com/release/{info.get('id')}",
            "date_added": item.data.get("date_added", ""),
        }

    def _serialize_release(self, release) -> dict:
        """Serialize a full Release object (triggers lazy loading)."""
        try:
            artists = [a.name for a in release.artists]
        except Exception:
            artists = []
        try:
            labels = [la.name for la in release.labels]
        except Exception:
            labels = []
        try:
            tracklist = [
                {"position": t.position, "title": t.title, "duration": t.duration}
                for t in release.tracklist
            ]
        except Exception:
            tracklist = []
        try:
            images = [img.get("uri", img.get("uri150", "")) for img in release.images] if release.images else []
        except Exception:
            images = []

        return {
            "id": release.id,
            "title": release.title,
            "year": getattr(release, "year", None),
            "artists": artists,
            "genres": getattr(release, "genres", []) or [],
            "styles": getattr(release, "styles", []) or [],
            "labels": labels,
            "formats": [f.get("name", "") for f in (getattr(release, "formats", []) or [])],
            "tracklist": tracklist,
            "images": images,
            "thumb": getattr(release, "thumb", ""),
            "country": getattr(release, "country", ""),
            "notes": getattr(release, "notes", ""),
            "num_for_sale": getattr(release, "num_for_sale", None),
            "lowest_price": getattr(release, "lowest_price", None),
            "url": f"https://www.discogs.com/release/{release.id}",
        }

    def _serialize_search_result(self, item) -> dict:
        """Serialize a search result item."""
        data = item.data if hasattr(item, "data") else {}
        title = data.get("title", str(item))
        # Search results have "Artist - Title" format in the title field
        parts = title.split(" - ", 1)
        if len(parts) == 2:
            artist_name, release_title = parts
        else:
            artist_name = ""
            release_title = title

        return {
            "id": data.get("id"),
            "title": release_title,
            "artists": [artist_name] if artist_name else [],
            "year": data.get("year"),
            "genres": data.get("genre", []),
            "styles": data.get("style", []),
            "labels": [la.get("name", la) if isinstance(la, dict) else la
                       for la in data.get("label", [])],
            "formats": data.get("format", []),
            "thumb": data.get("thumb", ""),
            "cover_image": data.get("cover_image", ""),
            "url": f"https://www.discogs.com{data.get('uri', '')}",
            "type": data.get("type", ""),
        }

    def _rate_limited_call(self, func, *args, **kwargs):
        """Handle 429 rate limit errors with exponential backoff."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except HTTPError as e:
                if e.status_code == 429 and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    time.sleep(wait)
                else:
                    raise
