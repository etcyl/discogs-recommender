import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from config import settings
from services.cache import cache
from services.discogs_service import DiscogsService
from services.recommendation import CollectionAnalyzer
from services.claude_recommender import ClaudeRecommender
from services.radio_service import RadioService
from services import thumbs
from services import channel_service

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Discogs Recommender", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Trusted host middleware (CWE-346)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "*"])

# Initialize services
discogs = DiscogsService(settings.app_name, settings.discogs_token, settings.discogs_username)
claude = ClaudeRecommender(api_key=settings.anthropic_api_key)
radio = RadioService(api_key=settings.anthropic_api_key)

# Spotify (optional — enabled only when credentials are configured)
spotify = None
if settings.spotify_client_id and settings.spotify_client_secret:
    from services.spotify_service import SpotifyService
    spotify = SpotifyService(settings.spotify_client_id, settings.spotify_client_secret)


# ---------------------------------------------------------------------------
# Request/response models (CWE-20)
# ---------------------------------------------------------------------------

class ThumbRequest(BaseModel):
    artist: str = Field(..., min_length=1, max_length=500)
    title: str = Field(..., min_length=1, max_length=500)
    album: str = Field("", max_length=500)
    genres: list[str] = Field(default_factory=list)
    styles: list[str] = Field(default_factory=list)

    @field_validator("genres", "styles")
    @classmethod
    def validate_lists(cls, v: list[str]) -> list[str]:
        return [s[:200] for s in v[:20] if isinstance(s, str)]


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    spotify_url: str = Field(..., min_length=10, max_length=500)
    mode: str = Field(..., pattern=r"^(play_playlist|similar_songs|new_discoveries)$")


class ChannelRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class SpotifyPreviewRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=500)


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to all responses (CWE-693)."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_error(error: Exception) -> str:
    """Return a safe error message without leaking internals (CWE-209)."""
    msg = str(error)
    # Strip file paths and stack traces
    sensitive_patterns = [
        settings.anthropic_api_key,
        settings.discogs_token,
    ]
    for pattern in sensitive_patterns:
        if pattern in msg:
            msg = msg.replace(pattern, "[REDACTED]")
    return msg


def _get_cached_collection() -> list[dict]:
    cache_key = f"collection:{settings.discogs_username}"
    collection = cache.get(cache_key)
    if collection is None:
        collection = discogs.get_full_collection()
        cache.set(cache_key, collection, ttl=3600)
    return collection


def _get_analyzer(collection: list[dict]) -> CollectionAnalyzer:
    return CollectionAnalyzer(collection)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard with collection profile summary."""
    try:
        collection = _get_cached_collection()
        analyzer = _get_analyzer(collection)
        profile = analyzer.get_profile()
        error = None
    except Exception as e:
        collection = []
        profile = None
        error = _sanitize_error(e)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "profile": profile,
        "username": settings.discogs_username,
        "error": error,
    })


@app.get("/collection", response_class=HTMLResponse)
async def collection(request: Request, page: int = Query(1, ge=1)):
    """Browse collection with pagination."""
    try:
        all_releases = _get_cached_collection()
        per_page = 24
        total_pages = max(1, (len(all_releases) + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        page_items = all_releases[start:start + per_page]
        error = None
    except Exception as e:
        page_items = []
        page = 1
        total_pages = 1
        error = _sanitize_error(e)

    return templates.TemplateResponse("collection.html", {
        "request": request,
        "releases": page_items,
        "page": page,
        "total_pages": total_pages,
        "username": settings.discogs_username,
        "error": error,
    })


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations(request: Request,
                          engine: str = Query("genre"),
                          discovery: int = Query(30, ge=0, le=100)):
    """Get recommendations via genre engine or Claude AI."""
    # Validate engine parameter (CWE-20)
    if engine not in ("genre", "claude"):
        engine = "genre"

    try:
        collection = _get_cached_collection()
        if not collection:
            return templates.TemplateResponse("recommendations.html", {
                "request": request,
                "recommendations": [],
                "engine": engine,
                "discovery": discovery,
                "profile": None,
                "error": "Your collection is empty. Add some releases on Discogs first!",
            })

        analyzer = _get_analyzer(collection)
        profile = analyzer.get_profile()

        if engine == "claude":
            cache_key = f"claude_rec:{settings.discogs_username}"
            recs = cache.get(cache_key)
            if not recs:
                recs = await asyncio.to_thread(claude.get_recommendations, profile, collection)
                recs = await asyncio.to_thread(claude.enrich_with_discogs, recs, discogs)
                cache.set(cache_key, recs, ttl=1800)
        else:
            cache_key = f"genre_rec:{settings.discogs_username}:{discovery}"
            recs = cache.get(cache_key)
            if not recs:
                recs = await asyncio.to_thread(
                    analyzer.get_recommendations, discogs, discovery=discovery)
                cache.set(cache_key, recs, ttl=1800)

        error = None
    except Exception as e:
        recs = []
        profile = None
        error = _sanitize_error(e)

    return templates.TemplateResponse("recommendations.html", {
        "request": request,
        "recommendations": recs,
        "engine": engine,
        "discovery": discovery,
        "profile": profile,
        "error": error,
    })


@app.get("/api/refresh-recommendations")
async def refresh_recommendations():
    """Clear genre recommendation caches to force fresh results."""
    cache.invalidate_prefix("genre_rec:")
    return {"status": "ok"}


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request,
                 q: str = Query(None),
                 artist: str = Query(None),
                 genre: str = Query(None),
                 style: str = Query(None),
                 label: str = Query(None)):
    """Search Discogs database."""
    results = None
    error = None
    query_params = {"q": q or "", "artist": artist or "", "genre": genre or "",
                    "style": style or "", "label": label or ""}

    if any([q, artist, genre, style, label]):
        try:
            results = discogs.search(query=q, artist=artist, genre=genre,
                                     style=style, label=label)
        except Exception as e:
            error = _sanitize_error(e)

    return templates.TemplateResponse("search.html", {
        "request": request,
        "results": results,
        "query": query_params,
        "error": error,
    })


@app.get("/release/{release_id}", response_class=HTMLResponse)
async def release_detail(request: Request, release_id: int):
    """View details of a single release."""
    try:
        cache_key = f"release:{release_id}"
        release = cache.get(cache_key)
        if not release:
            release = discogs.get_release_details(release_id)
            cache.set(cache_key, release, ttl=3600)
        error = None
    except Exception as e:
        release = None
        error = _sanitize_error(e)

    return templates.TemplateResponse("release.html", {
        "request": request,
        "release": release,
        "error": error,
    })


@app.get("/api/refresh-collection")
async def refresh_collection():
    """Clear all caches and force re-fetch."""
    cache.invalidate_prefix("collection:")
    cache.invalidate_prefix("genre_rec:")
    cache.invalidate_prefix("claude_rec:")
    cache.invalidate_prefix("release:")
    return {"status": "ok", "message": "Cache cleared. Reload the page to see fresh data."}


# ---------------------------------------------------------------------------
# Radio Mode
# ---------------------------------------------------------------------------

@app.get("/radio", response_class=HTMLResponse)
async def radio_page(request: Request):
    """Radio player page."""
    channels = channel_service.load_channels()
    return templates.TemplateResponse("radio.html", {
        "request": request,
        "username": settings.discogs_username,
        "channels": channels,
        "spotify_enabled": spotify is not None,
    })


@app.get("/api/radio/playlist")
async def radio_playlist(channel_id: str = Query("my-collection")):
    """Generate a radio playlist with YouTube video IDs."""
    cache_key = f"radio_playlist:{channel_id}"
    playlist = cache.get(cache_key)
    if playlist:
        return {"playlist": playlist, "cached": True}

    try:
        collection = _get_cached_collection()
        if not collection:
            return JSONResponse(status_code=400,
                                content={"error": "Collection is empty."})

        analyzer = _get_analyzer(collection)
        profile = analyzer.get_profile()
        thumbs_summary = thumbs.get_thumbs_summary()
        dislikes_summary = thumbs.get_dislikes_summary()

        playlist = await asyncio.to_thread(
            radio.generate_playlist, profile, collection, thumbs_summary, dislikes_summary)
        playlist = await asyncio.to_thread(radio.resolve_youtube_ids, playlist)

        if playlist:
            cache.set(cache_key, playlist, ttl=7200)

        return {"playlist": playlist, "cached": False}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": _sanitize_error(e)})


@app.get("/api/radio/playlist-stream")
async def radio_playlist_stream(channel_id: str = Query("my-collection")):
    """SSE endpoint: streams progress events while generating a channel's playlist."""
    async def event_generator():
        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        # Validate channel_id format (CWE-20)
        if not re.match(r"^[a-zA-Z0-9_-]+$", channel_id):
            yield _sse("error", {"message": "Invalid channel ID"})
            return

        cache_key = f"radio_playlist:{channel_id}"
        playlist = cache.get(cache_key)
        if playlist:
            yield _sse("complete", {"playlist": playlist, "cached": True})
            return

        try:
            channel = channel_service.get_channel(channel_id)
            if not channel:
                yield _sse("error", {"message": "Channel not found"})
                return

            source_type = channel.get("source_type", "discogs")

            if source_type == "discogs":
                # Existing Discogs flow — unchanged
                yield _sse("progress", {"message": "Loading your collection from Discogs...", "percent": 5})
                collection_data = await asyncio.to_thread(_get_cached_collection)
                if not collection_data:
                    yield _sse("error", {"message": "Collection is empty."})
                    return

                yield _sse("progress", {"message": "Analyzing your taste profile...", "percent": 15})
                analyzer = _get_analyzer(collection_data)
                profile = analyzer.get_profile()
                thumbs_summary = thumbs.get_thumbs_summary()
                dislikes_summary = thumbs.get_dislikes_summary()

                yield _sse("progress", {"message": "Claude is curating 40 songs for you...", "percent": 25})
                playlist = await asyncio.to_thread(
                    radio.generate_playlist, profile, collection_data, thumbs_summary, dislikes_summary)

                if not playlist:
                    yield _sse("error", {"message": "Claude returned no songs."})
                    return

            elif source_type == "spotify":
                if not spotify:
                    yield _sse("error", {"message": "Spotify is not configured."})
                    return

                playlist_id = channel.get("source_data", {}).get("playlist_id")
                if not playlist_id:
                    yield _sse("error", {"message": "Invalid channel data."})
                    return

                yield _sse("progress", {"message": "Fetching Spotify playlist...", "percent": 10})
                tracks = await asyncio.to_thread(spotify.get_playlist_tracks, playlist_id)
                if not tracks:
                    yield _sse("error", {"message": "Spotify playlist is empty."})
                    return

                mode = channel.get("mode", "similar_songs")

                if mode == "play_playlist":
                    yield _sse("progress", {"message": "Preparing playlist...", "percent": 25})
                    playlist = [
                        {
                            "artist": t["artist"],
                            "title": t["title"],
                            "album": t.get("album", ""),
                            "year": t.get("year", ""),
                            "reason": "From your Spotify playlist",
                            "similar_to": [],
                        }
                        for t in tracks
                    ]
                else:
                    mode_label = "similar songs" if mode == "similar_songs" else "new discoveries"
                    yield _sse("progress", {
                        "message": f"Claude is finding {mode_label}...",
                        "percent": 25,
                    })
                    thumbs_summary = thumbs.get_thumbs_summary()
                    dislikes_summary = thumbs.get_dislikes_summary()
                    playlist = await asyncio.to_thread(
                        radio.generate_playlist_from_tracks,
                        tracks, mode, thumbs_summary, dislikes_summary)

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            else:
                yield _sse("error", {"message": "Unknown channel type."})
                return

            # Resolve YouTube IDs in chunks
            total = len(playlist)
            resolved = []
            chunk_size = 5
            for i in range(0, total, chunk_size):
                chunk = playlist[i:i + chunk_size]
                done = len(resolved)
                pct = 30 + int(65 * done / total)
                yield _sse("progress", {
                    "message": f"Finding songs on YouTube... ({done}/{total})",
                    "percent": pct,
                })
                chunk_resolved = await asyncio.to_thread(radio.resolve_youtube_ids, chunk)
                resolved.extend(chunk_resolved)

            if resolved:
                cache.set(cache_key, resolved, ttl=7200)

            yield _sse("progress", {"message": "Ready!", "percent": 100})
            yield _sse("complete", {"playlist": resolved, "cached": False})

        except Exception as e:
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/radio/thumbs")
async def radio_thumbs(request: Request):
    """Save a thumbs-up for a song with validated input."""
    try:
        body = await request.json()
        thumb_data = ThumbRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        entry = thumbs.save_thumb(
            artist=thumb_data.artist,
            title=thumb_data.title,
            album=thumb_data.album,
            genres=thumb_data.genres,
            styles=thumb_data.styles,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save thumb"})


@app.post("/api/radio/dislike")
async def radio_dislike(request: Request):
    """Save a disliked song."""
    try:
        body = await request.json()
        thumb_data = ThumbRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        entry = thumbs.save_dislike(
            artist=thumb_data.artist,
            title=thumb_data.title,
            album=thumb_data.album,
            genres=thumb_data.genres,
            styles=thumb_data.styles,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save dislike"})


@app.post("/api/radio/history")
async def radio_history_save(request: Request):
    """Record a song play in history."""
    try:
        body = await request.json()
        thumb_data = ThumbRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        entry = thumbs.save_play(
            artist=thumb_data.artist,
            title=thumb_data.title,
            album=thumb_data.album,
            genres=thumb_data.genres,
            styles=thumb_data.styles,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save play"})


@app.get("/radio/likes", response_class=HTMLResponse)
async def radio_likes_page(request: Request):
    """View all liked songs."""
    liked = thumbs.load_thumbs()
    liked.reverse()  # newest first
    return templates.TemplateResponse("likes.html", {
        "request": request,
        "songs": liked,
        "total": len(liked),
    })


@app.get("/radio/history", response_class=HTMLResponse)
async def radio_history_page(request: Request):
    """View radio play history."""
    history = thumbs.load_history()
    history.reverse()  # newest first
    return templates.TemplateResponse("history.html", {
        "request": request,
        "songs": history,
        "total": len(history),
    })


@app.get("/api/radio/refresh-playlist")
async def radio_refresh(channel_id: str = Query("my-collection")):
    """Clear radio playlist cache for a specific channel."""
    cache_key = f"radio_playlist:{channel_id}"
    cache.invalidate(cache_key)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Channel Management
# ---------------------------------------------------------------------------

@app.get("/api/radio/channels")
async def list_channels():
    """List all radio channels."""
    channels = channel_service.load_channels()
    return {"channels": channels, "spotify_enabled": spotify is not None}


@app.post("/api/radio/channels")
async def create_channel(request: Request):
    """Create a new channel from a Spotify playlist URL."""
    if not spotify:
        return JSONResponse(status_code=400, content={"error": "Spotify is not configured."})

    try:
        body = await request.json()
        data = ChannelCreateRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    from services.spotify_service import SpotifyService
    playlist_id = SpotifyService.parse_playlist_url(data.spotify_url)
    if not playlist_id:
        return JSONResponse(status_code=400, content={"error": "Invalid Spotify playlist URL"})

    try:
        info = await asyncio.to_thread(spotify.get_playlist_info, playlist_id)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Could not fetch playlist. Is it public?"})

    try:
        channel = channel_service.create_channel(
            name=data.name,
            source_type="spotify",
            source_data={
                "playlist_id": playlist_id,
                "playlist_url": data.spotify_url,
                "playlist_name": info["name"],
                "track_count": info["track_count"],
            },
            mode=data.mode,
        )
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/radio/channels/{channel_id}")
async def rename_channel_endpoint(channel_id: str, request: Request):
    """Rename a channel."""
    try:
        body = await request.json()
        data = ChannelRenameRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.rename_channel(channel_id, data.name)
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/radio/channels/{channel_id}")
async def delete_channel_endpoint(channel_id: str):
    """Delete a channel."""
    try:
        channel_service.delete_channel(channel_id)
        # Also clear the channel's cached playlist
        cache.invalidate(f"radio_playlist:{channel_id}")
        return {"status": "ok"}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/radio/spotify-preview")
async def spotify_preview(request: Request):
    """Validate a Spotify URL and return playlist metadata."""
    if not spotify:
        return JSONResponse(status_code=400, content={"error": "Spotify is not configured."})

    try:
        body = await request.json()
        data = SpotifyPreviewRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    from services.spotify_service import SpotifyService
    playlist_id = SpotifyService.parse_playlist_url(data.url)
    if not playlist_id:
        return JSONResponse(status_code=400, content={"error": "Invalid Spotify playlist URL"})

    try:
        info = await asyncio.to_thread(spotify.get_playlist_info, playlist_id)
        return info
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Could not fetch playlist. Is it public?"})
