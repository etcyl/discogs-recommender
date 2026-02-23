import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request, Query, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from config import settings
from services.cache import cache
from services.discogs_service import DiscogsService
from services.recommendation import CollectionAnalyzer
from services.claude_recommender import ClaudeRecommender
from services.radio_service import RadioService
from services.llm_provider import LLMError, parse_llm_json
from services import thumbs
from services import channel_service
from services.database import init_db
from services import auth_service

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Discogs Recommender", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Trusted host middleware (CWE-346)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "*"])

# Initialize services
discogs = DiscogsService(settings.app_name, settings.discogs_token, settings.discogs_username)
claude = ClaudeRecommender(
    api_key=settings.anthropic_api_key,
    ollama_base_url=settings.ollama_base_url,
    ollama_model=settings.ollama_model,
)
radio = RadioService(
    anthropic_api_key=settings.anthropic_api_key,
    ollama_base_url=settings.ollama_base_url,
    ollama_model=settings.ollama_model,
)

AI_MODEL_LABELS = {
    "claude-sonnet": "Claude Sonnet",
    "claude-haiku": "Claude Haiku",
    "ollama": "Ollama",
}

# Spotify (optional — enabled only when credentials are configured)
spotify = None
if settings.spotify_client_id and settings.spotify_client_secret:
    from services.spotify_service import SpotifyService
    spotify = SpotifyService(settings.spotify_client_id, settings.spotify_client_secret)

# Initialize database and bootstrap admin user
init_db()
admin_user = auth_service.ensure_admin_exists()
auth_service.migrate_admin_data()
auth_service.cleanup_expired_sessions()


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
    spotify_url: str = Field("", max_length=500)
    theme: str = Field("", max_length=300)
    mode: str = Field(..., pattern=r"^(play_playlist|similar_songs|new_discoveries|themed)$")


class ChannelRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ChannelDiscoveryRequest(BaseModel):
    discovery: int = Field(..., ge=0, le=100)


class ChannelEraRequest(BaseModel):
    era_from: int | None = Field(None, ge=1900, le=2099)
    era_to: int | None = Field(None, ge=1900, le=2099)


class ChannelAiModelRequest(BaseModel):
    ai_model: str = Field(..., pattern=r"^(claude-sonnet|claude-haiku|ollama)$")


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
# Auth middleware
# ---------------------------------------------------------------------------

PUBLIC_PATHS = {"/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/invite/", "/static/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check session cookie and attach user to request state."""
    path = request.url.path

    # Allow public paths through without auth
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        request.state.user = None
        return await call_next(request)

    # Check session cookie
    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id:
        user = auth_service.validate_session(session_id)
        if user:
            request.state.user = user
            return await call_next(request)

    # Not authenticated
    return RedirectResponse(url="/login", status_code=302)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_error(error: Exception) -> str:
    """Return a safe error message without leaking internals (CWE-209)."""
    msg = str(error)
    sensitive_patterns = [
        settings.discogs_token,
    ]
    if settings.anthropic_api_key:
        sensitive_patterns.append(settings.anthropic_api_key)
    for pattern in sensitive_patterns:
        if pattern in msg:
            msg = msg.replace(pattern, "[REDACTED]")
    return msg


def _get_user_data_dir(user: dict) -> Path:
    """Return the per-user data directory, creating it if needed."""
    user_dir = BASE_DIR / "data" / user["id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _get_user_discogs(user: dict) -> DiscogsService:
    """Return a DiscogsService for this user. Falls back to admin's if no creds."""
    if user.get("discogs_username") and user.get("discogs_token"):
        cache_key = f"discogs_service:{user['id']}"
        svc = cache.get(cache_key)
        if not svc:
            svc = DiscogsService(settings.app_name, user["discogs_token"],
                                 user["discogs_username"])
            cache.set(cache_key, svc, ttl=3600)
        return svc
    return discogs


def _get_user_collection(user: dict) -> list[dict]:
    """Cached collection fetch, scoped to user's Discogs account."""
    svc = _get_user_discogs(user)
    cache_key = f"collection:{svc.username}"
    collection = cache.get(cache_key)
    if collection is None:
        collection = svc.get_full_collection()
        cache.set(cache_key, collection, ttl=3600)
    return collection


def _get_user_username(user: dict) -> str:
    """Get the effective Discogs username for display."""
    return user.get("discogs_username") or settings.discogs_username


def _get_analyzer(collection: list[dict]) -> CollectionAnalyzer:
    return CollectionAnalyzer(collection)


def _template_context(request: Request, **kwargs) -> dict:
    """Build standard template context with user info."""
    ctx = {"request": request}
    user = getattr(request.state, "user", None)
    if user:
        ctx["user"] = user
        ctx["username"] = _get_user_username(user)
    else:
        ctx["username"] = settings.discogs_username
    ctx.update(kwargs)
    return ctx


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login page."""
    # If already logged in, redirect home
    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id and auth_service.validate_session(session_id):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_submit(request: Request):
    """Admin login with Discogs token."""
    form = await request.form()
    token = str(form.get("discogs_token", "")).strip()

    if not token:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Please enter your Discogs personal access token.",
        })

    admin = auth_service.get_admin_user()
    if admin and admin.get("discogs_token") == token:
        session_id = auth_service.create_session(admin["id"])
        response = RedirectResponse(url="/", status_code=302)
        auth_service.set_session_cookie(response, session_id)
        return response

    return templates.TemplateResponse("login.html", {
        "request": request, "error": "Invalid token.",
    })


@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    """Show invite acceptance / setup page."""
    invite = auth_service.get_invite(token)
    if not invite:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "This invite link is invalid or has expired.",
        })
    return templates.TemplateResponse("setup.html", {
        "request": request, "token": token,
    })


@app.post("/invite/{token}")
async def invite_accept(request: Request, token: str):
    """Process invite acceptance: create user, set session, redirect."""
    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()[:100]
    discogs_username = str(form.get("discogs_username", "")).strip()[:100]
    discogs_token = str(form.get("discogs_token", "")).strip()[:200]

    if not display_name:
        return templates.TemplateResponse("setup.html", {
            "request": request, "token": token,
            "error": "Display name is required.",
        })

    try:
        user = auth_service.create_user_from_invite(
            token=token,
            display_name=display_name,
            discogs_username=discogs_username or "",
            discogs_token=discogs_token or "",
        )
    except ValueError as e:
        return templates.TemplateResponse("setup.html", {
            "request": request, "token": token, "error": str(e),
        })

    session_id = auth_service.create_session(user["id"])
    response = RedirectResponse(url="/", status_code=302)
    auth_service.set_session_cookie(response, session_id)
    return response


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id:
        auth_service.delete_session(session_id)
    response = RedirectResponse(url="/login", status_code=302)
    auth_service.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin panel for managing invites and users."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)
    invites = auth_service.list_invites(user["id"])
    users = auth_service.list_users()
    return templates.TemplateResponse("admin.html",
                                      _template_context(request, invites=invites, users=users))


@app.post("/admin/invite")
async def admin_create_invite(request: Request):
    """Generate a new invite link."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    label = str(body.get("label", "")).strip()[:100]
    token = auth_service.create_invite(user["id"], label=label)
    return {"status": "ok", "token": token, "url": f"/invite/{token}"}


@app.post("/admin/revoke-invite")
async def admin_revoke_invite(request: Request):
    """Revoke an invite token."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    auth_service.revoke_invite(body.get("token", ""))
    return {"status": "ok"}


@app.post("/admin/update-invite-label")
async def admin_update_invite_label(request: Request):
    """Update an invite's label."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    auth_service.update_invite_label(body.get("token", ""), body.get("label", ""))
    return {"status": "ok"}


@app.post("/admin/suspend-user")
async def admin_suspend_user(request: Request):
    """Suspend a user (revoke access without deleting)."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    auth_service.suspend_user(body.get("user_id", ""))
    return {"status": "ok"}


@app.post("/admin/unsuspend-user")
async def admin_unsuspend_user(request: Request):
    """Re-enable a suspended user."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    auth_service.unsuspend_user(body.get("user_id", ""))
    return {"status": "ok"}


@app.post("/admin/delete-user")
async def admin_delete_user(request: Request):
    """Delete a user entirely."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    try:
        auth_service.delete_user(body.get("user_id", ""))
        return {"status": "ok"}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/admin/rename-user")
async def admin_rename_user(request: Request):
    """Rename a user."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    auth_service.rename_user(body.get("user_id", ""), body.get("name", ""))
    return {"status": "ok"}


@app.post("/admin/update-user-models")
async def admin_update_user_models(request: Request):
    """Update which AI models a user can access."""
    user = request.state.user
    if not user or not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})
    body = await request.json()
    try:
        auth_service.update_user_allowed_models(
            body.get("user_id", ""), body.get("allowed_models", "all"))
        return {"status": "ok"}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard with collection profile summary."""
    user = request.state.user
    try:
        collection = await asyncio.to_thread(_get_user_collection, user)
        analyzer = _get_analyzer(collection)
        profile = analyzer.get_profile()
        error = None
    except Exception as e:
        collection = []
        profile = None
        error = _sanitize_error(e)

    return templates.TemplateResponse("index.html",
                                      _template_context(request, profile=profile, error=error))


@app.get("/collection", response_class=HTMLResponse)
async def collection(request: Request, page: int = Query(1, ge=1)):
    """Browse collection with pagination."""
    user = request.state.user
    try:
        all_releases = await asyncio.to_thread(_get_user_collection, user)
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

    return templates.TemplateResponse("collection.html",
                                      _template_context(request, releases=page_items, page=page,
                                                        total_pages=total_pages, error=error))


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations(request: Request,
                          engine: str = Query("genre"),
                          discovery: int = Query(30, ge=0, le=100),
                          era_from: int | None = Query(None, ge=1900, le=2099),
                          era_to: int | None = Query(None, ge=1900, le=2099),
                          source: str = Query("collection")):
    """Get recommendations via genre engine or Claude AI."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    user_discogs = _get_user_discogs(user)

    if engine not in ("genre", "claude", "claude-haiku", "ollama"):
        engine = "genre"
    if source not in ("collection", "spotify", "upload"):
        source = "collection"

    engine_to_ai_model = {
        "claude": "claude-sonnet",
        "claude-haiku": "claude-haiku",
        "ollama": "ollama",
    }

    recs = []
    profile = None
    error = None

    try:
        if source in ("spotify", "upload"):
            tracks_cache_key = f"rec_source_tracks:{user['id']}"
            tracks = cache.get(tracks_cache_key)
            if not tracks:
                error = "No tracks loaded. Please provide a Spotify URL or upload a file first."
            else:
                era_suffix = f":{era_from or ''}:{era_to or ''}"
                rec_cache_key = f"rec_from_tracks:{user['id']}{era_suffix}"
                recs = cache.get(rec_cache_key)
                if not recs:
                    recs = await asyncio.to_thread(
                        claude.get_recommendations_from_tracks, tracks,
                        era_from=era_from, era_to=era_to)
                    recs = await asyncio.to_thread(claude.enrich_with_discogs, recs, user_discogs)
                    thumbs.save_recommendations(recs, source=f"claude-{source}", data_dir=user_dir)
                    cache.set(rec_cache_key, recs, ttl=3600)
            engine = "claude"
        else:
            collection_data = await asyncio.to_thread(_get_user_collection, user)
            if not collection_data:
                error = "Your collection is empty. Add some releases on Discogs first!"
            else:
                recently_recommended = thumbs.get_recently_recommended_artists(data_dir=user_dir)
                analyzer = CollectionAnalyzer(collection_data, recently_recommended=recently_recommended)
                profile = analyzer.get_profile()

                era_suffix = f":{era_from or ''}:{era_to or ''}"

                # Enforce per-user model access
                allowed = auth_service.get_allowed_models(user)
                if engine in engine_to_ai_model:
                    ai_model = engine_to_ai_model[engine]
                    if ai_model not in allowed:
                        engine = "genre"  # Fall back to genre engine
                if engine in engine_to_ai_model:
                    ai_model = engine_to_ai_model[engine]
                    cache_key = f"ai_rec:{user['id']}:{ai_model}{era_suffix}"
                    recs = cache.get(cache_key)
                    if not recs:
                        play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                        rec_history_summary = thumbs.get_rec_history_summary(data_dir=user_dir)
                        recs = await asyncio.to_thread(
                            claude.get_recommendations, profile, collection_data,
                            play_history_summary=play_history_summary,
                            rec_history_summary=rec_history_summary,
                            era_from=era_from, era_to=era_to,
                            ai_model=ai_model)
                        recs = await asyncio.to_thread(claude.enrich_with_discogs, recs, user_discogs)
                        thumbs.save_recommendations(recs, source=engine, data_dir=user_dir)
                        cache.set(cache_key, recs, ttl=7200)
                else:
                    cache_key = f"genre_rec:{user['id']}:{discovery}{era_suffix}"
                    recs = cache.get(cache_key)
                    if not recs:
                        recs = await asyncio.to_thread(
                            analyzer.get_recommendations, user_discogs, discovery=discovery,
                            era_from=era_from, era_to=era_to)
                        thumbs.save_recommendations(recs, source="genre", data_dir=user_dir)
                        cache.set(cache_key, recs, ttl=3600)
    except Exception as e:
        recs = []
        profile = None
        error = _sanitize_error(e)

    allowed_models = list(auth_service.get_allowed_models(user))
    return templates.TemplateResponse("recommendations.html",
                                      _template_context(request, recommendations=recs,
                                                        engine=engine, discovery=discovery,
                                                        era_from=era_from, era_to=era_to,
                                                        source=source, profile=profile,
                                                        error=error,
                                                        spotify_enabled=spotify is not None,
                                                        allowed_models=allowed_models))


@app.get("/api/refresh-recommendations")
async def refresh_recommendations(request: Request):
    """Clear recommendation caches to force fresh results."""
    user = request.state.user
    cache.invalidate_prefix(f"genre_rec:{user['id']}:")
    cache.invalidate_prefix(f"ai_rec:{user['id']}:")
    cache.invalidate_prefix(f"rec_from_tracks:{user['id']}")
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

    return templates.TemplateResponse("search.html",
                                      _template_context(request, results=results,
                                                        query=query_params, error=error))


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

    return templates.TemplateResponse("release.html",
                                      _template_context(request, release=release, error=error))


@app.get("/api/refresh-collection")
async def refresh_collection(request: Request):
    """Clear all caches and force re-fetch."""
    user = request.state.user
    username = _get_user_username(user)
    cache.invalidate_prefix(f"collection:{username}")
    cache.invalidate_prefix(f"genre_rec:{user['id']}:")
    cache.invalidate_prefix(f"ai_rec:{user['id']}:")
    cache.invalidate_prefix("release:")
    return {"status": "ok", "message": "Cache cleared. Reload the page to see fresh data."}


# ---------------------------------------------------------------------------
# Radio Mode
# ---------------------------------------------------------------------------

@app.get("/radio", response_class=HTMLResponse)
async def radio_page(request: Request):
    """Radio player page."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    channels = channel_service.load_channels(data_dir=user_dir)
    allowed_models = list(auth_service.get_allowed_models(user))
    return templates.TemplateResponse("radio.html",
                                      _template_context(request, channels=channels,
                                                        spotify_enabled=spotify is not None,
                                                        allowed_models=allowed_models))


@app.get("/api/radio/playlist")
async def radio_playlist(request: Request,
                         channel_id: str = Query("my-collection")):
    """Generate a radio playlist with YouTube video IDs."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    cache_key = f"radio_playlist:{user['id']}:{channel_id}"
    playlist = cache.get(cache_key)
    if playlist:
        return {"playlist": playlist, "cached": True}

    try:
        collection_data = await asyncio.to_thread(_get_user_collection, user)
        if not collection_data:
            return JSONResponse(status_code=400,
                                content={"error": "Collection is empty."})

        analyzer = _get_analyzer(collection_data)
        profile = analyzer.get_profile()
        thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
        dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
        play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)

        playlist = await asyncio.to_thread(
            radio.generate_playlist, profile, collection_data, thumbs_summary,
            dislikes_summary, play_history_summary)
        playlist = await asyncio.to_thread(radio.resolve_youtube_ids, playlist)

        if playlist:
            thumbs.save_recommendations(playlist, source="radio", data_dir=user_dir)
            cache.set(cache_key, playlist, ttl=14400)

        return {"playlist": playlist, "cached": False}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": _sanitize_error(e)})


@app.get("/api/radio/playlist-stream")
async def radio_playlist_stream(request: Request,
                                channel_id: str = Query("my-collection")):
    """SSE endpoint: streams progress events while generating a channel's playlist."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)

    async def event_generator():
        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        if not re.match(r"^[a-zA-Z0-9_-]+$", channel_id):
            yield _sse("error", {"message": "Invalid channel ID"})
            return

        cache_key = f"radio_playlist:{user['id']}:{channel_id}"
        playlist = cache.get(cache_key)
        if playlist:
            yield _sse("complete", {"playlist": playlist, "cached": True, "ai_model": ""})
            return

        try:
            channel = channel_service.get_channel(channel_id, data_dir=user_dir)
            if not channel:
                yield _sse("error", {"message": "Channel not found"})
                return

            source_type = channel.get("source_type", "discogs")
            ai_model = channel.get("ai_model", "claude-sonnet")
            model_label = AI_MODEL_LABELS.get(ai_model, ai_model)

            allowed = auth_service.get_allowed_models(user)
            if ai_model not in allowed:
                yield _sse("error", {"message": f"You don't have access to {model_label}. Change the channel's AI model."})
                return

            if source_type == "discogs":
                yield _sse("progress", {"message": "Loading your collection from Discogs...", "percent": 5})
                collection_data = await asyncio.to_thread(_get_user_collection, user)
                if not collection_data:
                    yield _sse("error", {"message": "Collection is empty."})
                    return

                yield _sse("progress", {"message": "Analyzing your taste profile...", "percent": 15})
                analyzer = _get_analyzer(collection_data)
                profile = analyzer.get_profile()
                thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
                dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
                play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                discovery = channel.get("discovery", 30)
                era_from = channel.get("era_from")
                era_to = channel.get("era_to")
                theme = channel.get("source_data", {}).get("theme", "")

                try:
                    if theme:
                        yield _sse("progress", {"message": f"{model_label} is curating \"{theme}\" songs...", "percent": 25})
                        playlist = await asyncio.to_thread(
                            radio.generate_themed_playlist, profile, collection_data,
                            theme, thumbs_summary, dislikes_summary, play_history_summary,
                            discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model)
                    else:
                        yield _sse("progress", {"message": f"{model_label} is curating 40 songs for you...", "percent": 25})
                        playlist = await asyncio.to_thread(
                            radio.generate_playlist, profile, collection_data, thumbs_summary,
                            dislikes_summary, play_history_summary, discovery,
                            era_from=era_from, era_to=era_to,
                            ai_model=ai_model)
                except LLMError as e:
                    yield _sse("error", {"message": str(e)})
                    return

                if not playlist:
                    yield _sse("error", {"message": f"{model_label} returned no songs."})
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
                    mode_label_text = "similar songs" if mode == "similar_songs" else "new discoveries"
                    discovery = channel.get("discovery", 30)
                    era_from = channel.get("era_from")
                    era_to = channel.get("era_to")
                    yield _sse("progress", {
                        "message": f"{model_label} is finding {mode_label_text}...",
                        "percent": 25,
                    })
                    thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
                    dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
                    play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                    try:
                        playlist = await asyncio.to_thread(
                            radio.generate_playlist_from_tracks,
                            tracks, mode, thumbs_summary, dislikes_summary,
                            play_history_summary, discovery,
                            era_from=era_from, era_to=era_to,
                            ai_model=ai_model)
                    except LLMError as e:
                        yield _sse("error", {"message": str(e)})
                        return

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            elif source_type == "upload":
                tracks = channel.get("source_data", {}).get("tracks", [])
                if not tracks:
                    yield _sse("error", {"message": "Upload channel has no tracks."})
                    return

                mode = channel.get("mode", "similar_songs")

                if mode == "play_playlist":
                    yield _sse("progress", {"message": "Preparing uploaded tracks...", "percent": 25})
                    playlist = [
                        {
                            "artist": t["artist"],
                            "title": t["title"],
                            "album": t.get("album", ""),
                            "year": t.get("year", ""),
                            "reason": "From your uploaded file",
                            "similar_to": [],
                        }
                        for t in tracks
                    ]
                else:
                    mode_label_text = "similar songs" if mode == "similar_songs" else "new discoveries"
                    discovery = channel.get("discovery", 30)
                    era_from = channel.get("era_from")
                    era_to = channel.get("era_to")
                    yield _sse("progress", {
                        "message": f"{model_label} is finding {mode_label_text} from your uploads...",
                        "percent": 25,
                    })
                    thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
                    dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
                    play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                    try:
                        playlist = await asyncio.to_thread(
                            radio.generate_playlist_from_tracks,
                            tracks, mode, thumbs_summary, dislikes_summary,
                            play_history_summary, discovery,
                            era_from=era_from, era_to=era_to,
                            ai_model=ai_model)
                    except LLMError as e:
                        yield _sse("error", {"message": str(e)})
                        return

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
                thumbs.save_recommendations(resolved, source="radio", data_dir=user_dir)
                cache.set(cache_key, resolved, ttl=28800)

            yield _sse("progress", {"message": "Ready!", "percent": 100})
            yield _sse("complete", {"playlist": resolved, "cached": False, "ai_model": model_label})

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
    user = request.state.user
    user_dir = _get_user_data_dir(user)
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
            data_dir=user_dir,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save thumb"})


@app.post("/api/radio/dislike")
async def radio_dislike(request: Request):
    """Save a disliked song."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
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
            data_dir=user_dir,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save dislike"})


@app.post("/api/radio/history")
async def radio_history_save(request: Request):
    """Record a song play in history."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
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
            data_dir=user_dir,
        )
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save play"})


@app.get("/radio/likes", response_class=HTMLResponse)
async def radio_likes_page(request: Request):
    """View all liked songs."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    liked = thumbs.load_thumbs(data_dir=user_dir)
    liked.reverse()  # newest first
    return templates.TemplateResponse("likes.html",
                                      _template_context(request, songs=liked, total=len(liked)))


@app.get("/radio/history", response_class=HTMLResponse)
async def radio_history_page(request: Request):
    """View radio play history."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    history = thumbs.load_history(data_dir=user_dir)
    history.reverse()  # newest first
    return templates.TemplateResponse("history.html",
                                      _template_context(request, songs=history, total=len(history)))


@app.get("/api/radio/refresh-playlist")
async def radio_refresh(request: Request,
                        channel_id: str = Query("my-collection")):
    """Clear radio playlist cache for a specific channel."""
    user = request.state.user
    cache_key = f"radio_playlist:{user['id']}:{channel_id}"
    cache.invalidate(cache_key)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Channel Management
# ---------------------------------------------------------------------------

@app.get("/api/radio/channels")
async def list_channels(request: Request):
    """List all radio channels."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    channels = channel_service.load_channels(data_dir=user_dir)
    return {"channels": channels, "spotify_enabled": spotify is not None}


@app.post("/api/radio/channels")
async def create_channel(request: Request):
    """Create a new channel from a Spotify playlist URL or a themed collection channel."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelCreateRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    # Themed collection channel (no Spotify needed)
    if data.mode == "themed":
        if not data.theme or not data.theme.strip():
            return JSONResponse(status_code=400, content={"error": "Theme is required for themed channels"})
        try:
            channel = channel_service.create_channel(
                name=data.name,
                source_type="discogs",
                source_data={"theme": data.theme.strip()},
                mode="themed",
                data_dir=user_dir,
            )
            return {"status": "ok", "channel": channel}
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    # Spotify channel
    if not spotify:
        return JSONResponse(status_code=400, content={"error": "Spotify is not configured."})

    if not data.spotify_url:
        return JSONResponse(status_code=400, content={"error": "Spotify URL is required"})

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
            data_dir=user_dir,
        )
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/radio/channels/{channel_id}")
async def rename_channel_endpoint(channel_id: str, request: Request):
    """Rename a channel."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelRenameRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.rename_channel(channel_id, data.name, data_dir=user_dir)
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/radio/channels/{channel_id}")
async def delete_channel_endpoint(channel_id: str, request: Request):
    """Delete a channel."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        channel_service.delete_channel(channel_id, data_dir=user_dir)
        cache.invalidate(f"radio_playlist:{user['id']}:{channel_id}")
        return {"status": "ok"}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/radio/channels/{channel_id}/discovery")
async def update_channel_discovery_endpoint(channel_id: str, request: Request):
    """Update a channel's discovery level."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelDiscoveryRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.update_channel_discovery(channel_id, data.discovery,
                                                           data_dir=user_dir)
        cache.invalidate(f"radio_playlist:{user['id']}:{channel_id}")
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/radio/channels/{channel_id}/era")
async def update_channel_era_endpoint(channel_id: str, request: Request):
    """Update a channel's era filter."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelEraRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.update_channel_era(channel_id, data.era_from, data.era_to,
                                                     data_dir=user_dir)
        cache.invalidate(f"radio_playlist:{user['id']}:{channel_id}")
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/radio/channels/{channel_id}/ai-model")
async def update_channel_ai_model_endpoint(channel_id: str, request: Request):
    """Update a channel's AI model provider."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelAiModelRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    allowed = auth_service.get_allowed_models(user)
    if data.ai_model not in allowed:
        return JSONResponse(status_code=403, content={
            "error": f"You don't have access to {AI_MODEL_LABELS.get(data.ai_model, data.ai_model)}"})

    try:
        channel = channel_service.update_channel_ai_model(channel_id, data.ai_model,
                                                          data_dir=user_dir)
        cache.invalidate(f"radio_playlist:{user['id']}:{channel_id}")
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/ollama/status")
async def ollama_status():
    """Check if Ollama is running and list available models."""
    import httpx as _httpx
    try:
        resp = _httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"available": True, "models": models}
    except Exception:
        pass
    return {"available": False, "models": []}


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


# ---------------------------------------------------------------------------
# Upload Channel
# ---------------------------------------------------------------------------

@app.post("/api/radio/upload-channel")
async def create_upload_channel(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=100),
    mode: str = Form(..., pattern=r"^(play_playlist|similar_songs|new_discoveries)$"),
):
    """Create a channel from an uploaded text/PDF file."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)

    from services.upload_service import (
        extract_text_from_pdf, parse_tracks_with_claude,
        UploadParseError, MAX_FILE_SIZE, ALLOWED_CONTENT_TYPES,
    )

    content_type = file.content_type or ""
    if content_type not in ALLOWED_CONTENT_TYPES:
        return JSONResponse(status_code=400, content={
            "error": f"Unsupported file type: {content_type}. Use .txt or .pdf files."
        })

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        return JSONResponse(status_code=400, content={"error": "File too large. Maximum 2 MB."})
    if len(file_bytes) == 0:
        return JSONResponse(status_code=400, content={"error": "File is empty."})

    try:
        if content_type == "application/pdf":
            text = await asyncio.to_thread(extract_text_from_pdf, file_bytes)
        else:
            text = file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "error": f"Could not read file: {str(e)[:200]}"
        })

    try:
        tracks = await asyncio.to_thread(
            parse_tracks_with_claude, text, settings.anthropic_api_key
        )
    except UploadParseError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to parse file content."})

    try:
        channel = channel_service.create_channel(
            name=name,
            source_type="upload",
            source_data={
                "filename": file.filename or "upload",
                "tracks": tracks,
                "track_count": len(tracks),
            },
            mode=mode,
            data_dir=user_dir,
        )
        return {"status": "ok", "channel": channel, "track_count": len(tracks)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Mindmap
# ---------------------------------------------------------------------------

@app.get("/api/mindmap/expand")
async def mindmap_expand(
    request: Request,
    artist: str = Query(..., min_length=1, max_length=300),
    album: str = Query("", max_length=300),
):
    """Return 3-5 related artists/albums for mindmap expansion."""
    user = request.state.user
    cache_key = f"mindmap:{artist.lower()}:{album.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        collection_data = await asyncio.to_thread(_get_user_collection, user)
    except Exception:
        collection_data = []

    collection_artists = set()
    for r in collection_data:
        for a in r.get("artists", []):
            collection_artists.add(a.lower())

    from services.llm_provider import call_llm as _call_llm

    prompt_system = "You suggest closely related artists and albums based on deep music connections."
    prompt_user = (
        f'Given the artist "{artist}" and album "{album}", suggest 3-5 closely related '
        f"artists and their best album. Focus on deep connections: shared producers, same "
        f"scene/label, direct influences, collaborators.\n\n"
        f'Return a JSON array of objects with keys: "artist", "album", "why"\n'
        f'The "why" should be 1 brief sentence about the specific connection.\n'
        f"Return ONLY the JSON array."
    )

    try:
        text = await asyncio.to_thread(
            _call_llm,
            system_prompt=prompt_system,
            user_prompt=prompt_user,
            provider="claude-sonnet",
            max_tokens=500,
            anthropic_api_key=settings.anthropic_api_key,
        )

        result = parse_llm_json(text)

        for item in result:
            item["in_collection"] = item.get("artist", "").lower() in collection_artists

        response = {"related": result}
        cache.set(cache_key, response, ttl=7200)
        return response
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": _sanitize_error(e)})


# ---------------------------------------------------------------------------
# Recommendation Sources (Spotify / Upload)
# ---------------------------------------------------------------------------

@app.post("/api/recommendations/load-tracks")
async def load_recommendation_tracks(request: Request):
    """Load tracks from a Spotify URL for the recommendations page."""
    user = request.state.user
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request"})

    source_type = body.get("source_type", "")

    if source_type == "spotify":
        if not spotify:
            return JSONResponse(status_code=400, content={"error": "Spotify not configured."})

        url = body.get("url", "")
        from services.spotify_service import SpotifyService
        playlist_id = SpotifyService.parse_playlist_url(url)
        if not playlist_id:
            return JSONResponse(status_code=400, content={"error": "Invalid Spotify URL"})

        try:
            tracks = await asyncio.to_thread(spotify.get_playlist_tracks, playlist_id)
            info = await asyncio.to_thread(spotify.get_playlist_info, playlist_id)
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Could not fetch playlist."})

        cache_key = f"rec_source_tracks:{user['id']}"
        cache.set(cache_key, tracks, ttl=3600)
        cache.invalidate_prefix(f"rec_from_tracks:{user['id']}")
        return {"status": "ok", "track_count": len(tracks), "name": info["name"]}

    return JSONResponse(status_code=400, content={"error": "Invalid source_type"})


@app.post("/api/recommendations/upload-tracks")
async def upload_recommendation_tracks(request: Request, file: UploadFile = File(...)):
    """Upload a file as the source for recommendations."""
    user = request.state.user

    from services.upload_service import (
        extract_text_from_pdf, parse_tracks_with_claude,
        UploadParseError, MAX_FILE_SIZE, ALLOWED_CONTENT_TYPES,
    )

    content_type = file.content_type or ""
    if content_type not in ALLOWED_CONTENT_TYPES:
        return JSONResponse(status_code=400, content={"error": "Unsupported file type. Use .txt or .pdf."})

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        return JSONResponse(status_code=400, content={"error": "File too large."})
    if not file_bytes:
        return JSONResponse(status_code=400, content={"error": "File is empty."})

    try:
        if content_type == "application/pdf":
            text = await asyncio.to_thread(extract_text_from_pdf, file_bytes)
        else:
            text = file_bytes.decode("utf-8", errors="replace")

        tracks = await asyncio.to_thread(
            parse_tracks_with_claude, text, settings.anthropic_api_key
        )
    except UploadParseError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to parse file."})

    cache_key = f"rec_source_tracks:{user['id']}"
    cache.set(cache_key, tracks, ttl=3600)
    cache.invalidate_prefix(f"rec_from_tracks:{user['id']}")
    return {"status": "ok", "track_count": len(tracks), "name": file.filename or "upload"}
