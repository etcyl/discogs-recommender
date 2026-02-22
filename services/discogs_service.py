import time
import discogs_client
from discogs_client.exceptions import HTTPError


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


class DiscogsService:
    def __init__(self, app_name: str, user_token: str, username: str):
        self.client = discogs_client.Client(app_name, user_token=user_token)
        self.username = username

    def get_collection_page(self, page: int = 1, per_page: int = 100) -> dict:
        """Fetch a single page of releases from folder 0 (All)."""
        per_page = min(max(1, per_page), MAX_PER_PAGE)
        page = max(1, page)
        me = self.client.identity()
        folder = me.collection_folders[0]
        releases = folder.releases
        releases.per_page = per_page
        page_data = releases.page(page)
        return {
            "items": [self._serialize_collection_item(item) for item in page_data],
            "page": page,
            "pages": releases.pages,
            "total": releases.count,
        }

    def get_full_collection(self) -> list[dict]:
        """Fetch ALL releases from the collection, paginating automatically."""
        all_releases = []
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
        release = self._rate_limited_call(self.client.release, release_id)
        return self._serialize_release(release)

    def search(self, query: str = None, type: str = "release",
               artist: str = None, genre: str = None,
               style: str = None, label: str = None,
               page: int = 1, per_page: int = 50) -> list[dict]:
        """Search the Discogs database with sanitized inputs."""
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

        results = self._rate_limited_call(self.client.search, **kwargs)
        results.per_page = per_page
        try:
            page_data = results.page(page)
        except Exception:
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
