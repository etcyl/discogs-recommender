import asyncio
import hmac
import json
import logging
import os
import queue as queue_mod
import random
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx as _httpx
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
APP_VERSION = (BASE_DIR / "VERSION").read_text().strip()

app = FastAPI(title="Discogs Recommender", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Trusted host middleware (CWE-346) — allow localhost + any *.trycloudflare.com tunnel
_ALLOWED_HOSTS = [
    "localhost", "127.0.0.1", "*.trycloudflare.com",
]
if os.environ.get("TESTING"):
    _ALLOWED_HOSTS.append("testserver")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

# Initialize services (Discogs is optional — app works without it)
if settings.discogs_configured:
    discogs = DiscogsService(settings.app_name, settings.discogs_token, settings.discogs_username)
else:
    discogs = None
    logger.info("No Discogs credentials configured — collection features disabled. "
                "Add DISCOGS_TOKEN and DISCOGS_USERNAME to .env to enable.")
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

# Spotify (no credentials needed — scrapes public embed pages)
from services.spotify_service import SpotifyService
spotify = SpotifyService()

# YouTube playlist import (no credentials needed — uses yt-dlp)
from services.youtube_playlist_service import YouTubePlaylistService, YouTubeServiceError
youtube_playlist = YouTubePlaylistService()

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


class FeedbackSongItem(BaseModel):
    artist: str = Field("", max_length=500)
    title: str = Field("", max_length=500)
    reason: str = Field("", max_length=1000)
    match_attributes: list[str] = Field(default_factory=list)
    similar_to: list = Field(default_factory=list)

    @field_validator("match_attributes")
    @classmethod
    def validate_attrs(cls, v: list) -> list[str]:
        return [str(s)[:200] for s in v[:10] if s]


class FeedbackRequest(BaseModel):
    channel_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", max_length=50)
    session_liked: list[FeedbackSongItem] = Field(default_factory=list)
    session_disliked: list[FeedbackSongItem] = Field(default_factory=list)
    current_queue: list[FeedbackSongItem] = Field(default_factory=list)
    num_replacements: int = Field(8, ge=3, le=15)

    @field_validator("session_liked", "session_disliked", "current_queue")
    @classmethod
    def limit_list_size(cls, v: list) -> list:
        return v[:50]


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    spotify_url: str = Field("", max_length=500)
    theme: str = Field("", max_length=300)
    mode: str = Field(..., pattern=r"^(play_playlist|similar_songs|new_discoveries|themed)$")
    ai_model: str = Field("claude-sonnet", pattern=r"^(claude-sonnet|claude-haiku|ollama)$")
    era: str = Field("", max_length=20)
    num_songs: int = Field(50, ge=5, le=100)


class ChannelRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ChannelDiscoveryRequest(BaseModel):
    discovery: int = Field(..., ge=0, le=100)


class ChannelEraRequest(BaseModel):
    era_from: int | None = Field(None, ge=1900, le=2099)
    era_to: int | None = Field(None, ge=1900, le=2099)


class ChannelAiModelRequest(BaseModel):
    ai_model: str = Field(..., pattern=r"^(claude-sonnet|claude-haiku|ollama)$")


class ChannelNumSongsRequest(BaseModel):
    num_songs: int = Field(..., ge=5, le=100)


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
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net https://www.youtube.com; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        "img-src 'self' data: https://i.discogs.com https://i.ytimg.com https://*.ytimg.com; "
        "media-src 'self' https://*.googlevideo.com https://*.youtube.com; "
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
        "connect-src 'self' https://www.youtube.com https://*.googlevideo.com"
    )
    return response


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, resets on restart)
# ---------------------------------------------------------------------------

_rate_limits: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(key: str, max_requests: int = 10, window_seconds: int = 60) -> bool:
    """Return True if the key has exceeded max_requests in the time window."""
    now = time.time()
    timestamps = _rate_limits[key]
    # Remove expired entries
    _rate_limits[key] = [t for t in timestamps if now - t < window_seconds]
    if len(_rate_limits[key]) >= max_requests:
        return True
    _rate_limits[key].append(now)
    return False


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

PUBLIC_PATHS = {"/login", "/favicon.ico", "/api/system/status"}
PUBLIC_PREFIXES = ("/invite/", "/static/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check session cookie and attach user to request state."""
    path = request.url.path

    # Allow public paths through without auth
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        request.state.user = None
        return await call_next(request)

    # Auto-login mode: when no Discogs credentials are configured,
    # automatically authenticate all visitors as the local admin (single-user mode)
    if not settings.discogs_configured:
        admin = auth_service.get_admin_user()
        if admin:
            request.state.user = admin
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

_ERA_MAP = {
    "60s": (1960, 1969), "70s": (1970, 1979), "80s": (1980, 1989),
    "90s": (1990, 1999), "00s": (2000, 2009), "10s": (2010, 2019),
    "20s": (2020, 2029),
}


def _parse_era(era: str) -> tuple:
    """Convert era string like '70s' or '1970-1979' into (era_from, era_to)."""
    if not era:
        return None, None
    if era in _ERA_MAP:
        return _ERA_MAP[era]
    if "-" in era:
        parts = era.split("-", 1)
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
    return None, None


def _sanitize_error(error: Exception) -> str:
    """Return a safe error message without leaking internals (CWE-209)."""
    msg = str(error)
    sensitive_patterns = []
    if settings.discogs_token:
        sensitive_patterns.append(settings.discogs_token)
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


def _get_user_discogs(user: dict) -> DiscogsService | None:
    """Return a DiscogsService for this user. Falls back to admin's if no creds.
    Returns None if no Discogs credentials are configured anywhere."""
    if user.get("discogs_username") and user.get("discogs_token"):
        cache_key = f"discogs_service:{user['id']}"
        svc = cache.get(cache_key)
        if not svc:
            svc = DiscogsService(settings.app_name, user["discogs_token"],
                                 user["discogs_username"])
            cache.set(cache_key, svc, ttl=3600)
        return svc
    return discogs  # May be None if no Discogs configured


def _get_user_collection(user: dict) -> list[dict]:
    """Cached collection fetch, scoped to user's Discogs account.
    Returns empty list if no Discogs credentials configured."""
    svc = _get_user_discogs(user)
    if svc is None:
        return []
    cache_key = f"collection:{svc.username}"
    collection = cache.get(cache_key)
    if collection is None:
        collection = svc.get_full_collection()
        cache.set(cache_key, collection, ttl=3600)
    return collection


def _get_user_username(user: dict) -> str:
    """Get the effective Discogs username for display."""
    username = user.get("discogs_username") or settings.discogs_username
    if username and username != "local":
        return username
    return user.get("display_name", "User")


def _get_analyzer(collection: list[dict]) -> CollectionAnalyzer:
    return CollectionAnalyzer(collection)


def _template_context(request: Request, **kwargs) -> dict:
    """Build standard template context with user info."""
    ctx = {"request": request, "app_version": APP_VERSION}
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
    """Show login page. Auto-login admin from localhost."""
    # If already logged in, redirect home
    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id and auth_service.validate_session(session_id):
        return RedirectResponse(url="/", status_code=302)

    # Auto-login admin when accessing directly from localhost (not through proxy)
    client_ip = request.client.host if request.client else ""
    is_proxied = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if not is_proxied and client_ip in ("127.0.0.1", "::1"):
        admin = auth_service.get_admin_user()
        if admin:
            new_session = auth_service.create_session(admin["id"])
            response = RedirectResponse(url="/", status_code=302)
            auth_service.set_session_cookie(response, new_session)
            return response

    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_submit(request: Request):
    """Admin login with Discogs token."""
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(f"login:{client_ip}", max_requests=5, window_seconds=60):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Too many login attempts. Please wait a minute.",
        })

    form = await request.form()
    token = str(form.get("discogs_token", "")).strip()

    if not token:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Please enter your Discogs personal access token.",
        })

    admin = auth_service.get_admin_user()
    if admin and hmac.compare_digest(admin.get("discogs_token", ""), token):
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
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(f"invite:{client_ip}", max_requests=5, window_seconds=60):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Too many attempts. Please wait a minute.",
        })

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
# System Status & Hardware Detection
# ---------------------------------------------------------------------------

@app.get("/api/system/status")
async def system_status():
    """Return which services are configured (for frontend alerts)."""
    from services.hardware_service import _check_ollama
    ollama_info = _check_ollama(settings.ollama_base_url)
    return {
        "discogs_configured": settings.discogs_configured,
        "anthropic_configured": settings.anthropic_configured,
        "ollama_available": ollama_info["running"],
        "ollama_models": ollama_info["models"],
        "ollama_installed": ollama_info["installed"],
    }


@app.get("/api/system/hardware")
async def system_hardware(request: Request):
    """Return hardware info for local model recommendations."""
    from services.hardware_service import get_hardware_info
    info = await asyncio.to_thread(get_hardware_info, settings.ollama_base_url)
    return info


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard with collection profile summary."""
    user = request.state.user
    profile = None
    error = None
    discogs_configured = settings.discogs_configured

    if discogs_configured:
        try:
            collection = await asyncio.to_thread(_get_user_collection, user)
            if collection:
                analyzer = _get_analyzer(collection)
                profile = analyzer.get_profile()
        except Exception as e:
            error = _sanitize_error(e)

    return templates.TemplateResponse("index.html",
                                      _template_context(request, profile=profile, error=error,
                                                        discogs_configured=discogs_configured))


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
    channels = channel_service.load_channels(
        data_dir=user_dir, discogs_configured=settings.discogs_configured)
    allowed_models = list(auth_service.get_allowed_models(user))
    return templates.TemplateResponse("radio.html",
                                      _template_context(request, channels=channels,
                                                        allowed_models=allowed_models,
                                                        discogs_configured=settings.discogs_configured))


@app.get("/api/radio/playlist")
async def radio_playlist(request: Request,
                         channel_id: str = Query("my-collection")):
    """Generate a radio playlist with YouTube video IDs."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    cache_key = f"radio_playlist:{user['id']}:{channel_id}"
    playlist = cache.get(cache_key)
    if playlist:
        # Post-cache filter: remove songs liked/disliked/played since cache was set
        filter_set = thumbs.get_dislikes_set(data_dir=user_dir)
        filter_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
        filter_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
        filter_set.update(thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir))
        playlist = [
            s for s in playlist
            if (s.get("artist", "").lower().strip(),
                s.get("title", "").lower().strip()) not in filter_set
        ]
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
        exclude_set = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
        exclude_set.update(thumbs.get_dislikes_set(data_dir=user_dir))
        exclude_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
        exclude_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))

        playlist = await asyncio.to_thread(
            radio.generate_playlist, profile, collection_data, thumbs_summary,
            dislikes_summary, play_history_summary, exclude_set=exclude_set)
        playlist = await asyncio.to_thread(radio.resolve_youtube_ids, playlist, exclude_set)

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

        async def _keepalive_loop(task, interval=10, progress_q=None):
            """Yield keepalives and progress SSE while a task runs, to keep the connection alive."""
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=interval)
                if progress_q:
                    while not progress_q.empty():
                        try:
                            yield progress_q.get_nowait()
                        except Exception:
                            break
                if not done:
                    yield ": keepalive\n\n"
            # Final drain
            if progress_q:
                while not progress_q.empty():
                    try:
                        yield progress_q.get_nowait()
                    except Exception:
                        break

        if not re.match(r"^[a-zA-Z0-9_-]+$", channel_id):
            yield _sse("error", {"message": "Invalid channel ID"})
            return

        cache_key = f"radio_playlist:{user['id']}:{channel_id}"
        playlist = cache.get(cache_key)
        if playlist:
            # Post-cache filter: remove songs liked/disliked/played since cache was set
            filter_set = thumbs.get_dislikes_set(data_dir=user_dir)
            filter_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
            filter_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
            filter_set.update(thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir))
            playlist = [
                s for s in playlist
                if (s.get("artist", "").lower().strip(),
                    s.get("title", "").lower().strip()) not in filter_set
            ]
            yield _sse("song", {"songs": playlist, "total_expected": len(playlist)})
            yield _sse("complete", {"cached": True, "ai_model": ""})
            return

        try:
            channel = channel_service.get_channel(channel_id, data_dir=user_dir)
            if not channel:
                yield _sse("error", {"message": "Channel not found"})
                return

            source_type = channel.get("source_type", "discogs")
            ai_model = channel.get("ai_model", "claude-sonnet")
            num_songs = channel.get("num_songs", 50)
            model_label = AI_MODEL_LABELS.get(ai_model, ai_model)

            allowed = auth_service.get_allowed_models(user)
            if ai_model not in allowed:
                yield _sse("error", {"message": f"You don't have access to {model_label}. Change the channel's AI model."})
                return

            if source_type == "discogs":
                if not settings.discogs_configured and not (user.get("discogs_username") and user.get("discogs_token")):
                    yield _sse("error", {"message": "No Discogs account connected. Add DISCOGS_TOKEN and DISCOGS_USERNAME to your .env file, or create a Spotify/YouTube/themed channel instead."})
                    return
                yield _sse("progress", {"message": "Loading your collection from Discogs...", "percent": 5})
                _task = asyncio.ensure_future(asyncio.to_thread(_get_user_collection, user))
                async for _p in _keepalive_loop(_task):
                    yield _p
                collection_data = _task.result()
                if not collection_data:
                    yield _sse("error", {"message": "Collection is empty."})
                    return

                yield _sse("progress", {"message": "Analyzing your taste profile...", "percent": 15})
                analyzer = _get_analyzer(collection_data)
                profile = analyzer.get_profile()
                thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
                dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
                play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                exclude_set = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
                exclude_set.update(thumbs.get_dislikes_set(data_dir=user_dir))
                exclude_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
                exclude_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
                discovery = channel.get("discovery", 30)
                era_from = channel.get("era_from")
                era_to = channel.get("era_to")
                theme = channel.get("source_data", {}).get("theme", "")

                try:
                    pq = queue_mod.Queue()
                    def _on_batch(collected, total):
                        pct = 25 + int(5 * collected / max(total, 1))
                        pq.put(_sse("progress", {"message": f"{model_label}: {collected}/{total} songs generated...", "percent": pct}))

                    if theme:
                        yield _sse("progress", {"message": f"{model_label} is curating \"{theme}\" songs...", "percent": 25})
                        coro = asyncio.to_thread(
                            radio.generate_themed_playlist, profile, collection_data,
                            theme, thumbs_summary, dislikes_summary, play_history_summary,
                            exclude_set=exclude_set,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs, on_batch=_on_batch)
                    else:
                        yield _sse("progress", {"message": f"{model_label} is curating {num_songs} songs for you...", "percent": 25})
                        coro = asyncio.to_thread(
                            radio.generate_playlist, profile, collection_data, thumbs_summary,
                            dislikes_summary, play_history_summary,
                            exclude_set=exclude_set,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs, on_batch=_on_batch)
                    _task = asyncio.ensure_future(coro)
                    async for _p in _keepalive_loop(_task, progress_q=pq):
                        yield _p
                    playlist = _task.result()
                except LLMError as e:
                    yield _sse("error", {"message": str(e)})
                    return

                if not playlist:
                    yield _sse("error", {"message": f"{model_label} returned no songs."})
                    return

            elif source_type == "spotify":
                playlist_id = channel.get("source_data", {}).get("playlist_id")
                if not playlist_id:
                    yield _sse("error", {"message": "Invalid channel data."})
                    return

                yield _sse("progress", {"message": "Fetching Spotify playlist...", "percent": 10})
                _task = asyncio.ensure_future(asyncio.to_thread(spotify.get_playlist_tracks, playlist_id))
                async for _p in _keepalive_loop(_task):
                    yield _p
                tracks = _task.result()
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
                    sp_exclude = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
                    sp_exclude.update(thumbs.get_dislikes_set(data_dir=user_dir))
                    sp_exclude.update(thumbs.get_thumbs_set(data_dir=user_dir))
                    sp_exclude.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
                    try:
                        pq = queue_mod.Queue()
                        def _on_batch_sp(collected, total):
                            pct = 25 + int(5 * collected / max(total, 1))
                            pq.put(_sse("progress", {"message": f"{model_label}: {collected}/{total} songs generated...", "percent": pct}))
                        _task = asyncio.ensure_future(asyncio.to_thread(
                            radio.generate_playlist_from_tracks,
                            tracks, mode, thumbs_summary, dislikes_summary,
                            play_history_summary, exclude_set=sp_exclude,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs,
                            on_batch=_on_batch_sp))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
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
                    up_exclude = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
                    up_exclude.update(thumbs.get_dislikes_set(data_dir=user_dir))
                    up_exclude.update(thumbs.get_thumbs_set(data_dir=user_dir))
                    up_exclude.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
                    try:
                        pq = queue_mod.Queue()
                        def _on_batch_up(collected, total):
                            pct = 25 + int(5 * collected / max(total, 1))
                            pq.put(_sse("progress", {"message": f"{model_label}: {collected}/{total} songs generated...", "percent": pct}))
                        _task = asyncio.ensure_future(asyncio.to_thread(
                            radio.generate_playlist_from_tracks,
                            tracks, mode, thumbs_summary, dislikes_summary,
                            play_history_summary, exclude_set=up_exclude,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs,
                            on_batch=_on_batch_up))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
                    except LLMError as e:
                        yield _sse("error", {"message": str(e)})
                        return

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            elif source_type == "youtube":
                tracks = channel.get("source_data", {}).get("tracks", [])
                if not tracks:
                    yield _sse("error", {"message": "YouTube channel has no tracks."})
                    return

                mode = channel.get("mode", "similar_songs")

                if mode == "play_playlist":
                    yield _sse("progress", {"message": "Preparing YouTube playlist...", "percent": 25})
                    playlist = [
                        {
                            "artist": t["artist"],
                            "title": t["title"],
                            "album": t.get("album", ""),
                            "year": t.get("year", ""),
                            "reason": "From your YouTube playlist",
                            "similar_to": [],
                            "videoId": t.get("videoId", ""),
                        }
                        for t in tracks
                    ]
                else:
                    mode_label_text = "similar songs" if mode == "similar_songs" else "new discoveries"
                    discovery = channel.get("discovery", 30)
                    era_from = channel.get("era_from")
                    era_to = channel.get("era_to")
                    yield _sse("progress", {
                        "message": f"{model_label} is finding {mode_label_text} from your YouTube playlist...",
                        "percent": 25,
                    })
                    thumbs_summary = thumbs.get_thumbs_summary(data_dir=user_dir)
                    dislikes_summary = thumbs.get_dislikes_summary(data_dir=user_dir)
                    play_history_summary = thumbs.get_play_history_summary(data_dir=user_dir)
                    yt_exclude = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
                    yt_exclude.update(thumbs.get_dislikes_set(data_dir=user_dir))
                    yt_exclude.update(thumbs.get_thumbs_set(data_dir=user_dir))
                    yt_exclude.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
                    try:
                        pq = queue_mod.Queue()
                        def _on_batch_yt(collected, total):
                            pct = 25 + int(5 * collected / max(total, 1))
                            pq.put(_sse("progress", {"message": f"{model_label}: {collected}/{total} songs generated...", "percent": pct}))
                        _task = asyncio.ensure_future(asyncio.to_thread(
                            radio.generate_playlist_from_tracks,
                            tracks, mode, thumbs_summary, dislikes_summary,
                            play_history_summary, exclude_set=yt_exclude,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs,
                            on_batch=_on_batch_yt))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
                    except LLMError as e:
                        yield _sse("error", {"message": str(e)})
                        return

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            elif source_type == "liked":
                liked_songs = thumbs.load_thumbs(data_dir=user_dir)
                if not liked_songs:
                    yield _sse("error", {"message": "No liked songs yet. Like some songs on the radio to build your playlist!"})
                    return

                yield _sse("progress", {"message": f"Shuffling {len(liked_songs)} liked songs...", "percent": 15})
                random.shuffle(liked_songs)
                playlist = [
                    {
                        "artist": t.get("artist", ""),
                        "title": t.get("title", ""),
                        "album": t.get("album", ""),
                        "year": "",
                        "reason": "From your liked songs",
                        "similar_to": [],
                        "genres": t.get("genres", []),
                        "styles": t.get("styles", []),
                    }
                    for t in liked_songs
                ]

                if not playlist:
                    yield _sse("error", {"message": "No songs to play."})
                    return

            else:
                yield _sse("error", {"message": "Unknown channel type."})
                return

            # Build exclude set for post-YouTube-resolution filtering
            # (YT title rewriting can change artist/title to match known songs)
            yt_filter_set = thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir)
            yt_filter_set.update(thumbs.get_dislikes_set(data_dir=user_dir))
            yt_filter_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
            yt_filter_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))

            # Resolve YouTube IDs in chunks, streaming each batch to the client
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
                _task = asyncio.ensure_future(asyncio.to_thread(radio.resolve_youtube_ids, chunk, yt_filter_set))
                async for _p in _keepalive_loop(_task):
                    yield _p
                batch_resolved = _task.result()
                if batch_resolved:
                    resolved.extend(batch_resolved)
                    yield _sse("song", {"songs": batch_resolved, "total_expected": total})

            if resolved:
                thumbs.save_recommendations(resolved, source="radio", data_dir=user_dir)
                ttl = 1800 if source_type == "liked" else 28800
                cache.set(cache_key, resolved, ttl=ttl)

            yield _sse("progress", {"message": "Ready!", "percent": 100})
            yield _sse("complete", {"cached": False, "ai_model": model_label})

        except Exception as e:
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
        },
    )


@app.get("/api/radio/liked-keys")
async def radio_liked_keys(request: Request):
    """Return all liked song keys for frontend pre-population."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    liked = thumbs.load_thumbs(data_dir=user_dir)
    keys = [f"{t.get('artist', '')}-{t.get('title', '')}".lower()
            for t in liked if t.get("artist") and t.get("title")]
    return {"keys": keys}


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


@app.post("/api/radio/feedback")
async def radio_feedback(request: Request):
    """Generate replacement songs based on in-session feedback."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = FeedbackRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    channel = channel_service.get_channel(data.channel_id, data_dir=user_dir)
    if not channel:
        return JSONResponse(status_code=404, content={"error": "Channel not found"})

    channel_context = {
        "discovery": channel.get("discovery", 30),
        "era_from": channel.get("era_from"),
        "era_to": channel.get("era_to"),
        "theme": channel.get("source_data", {}).get("theme", ""),
        "source_type": channel.get("source_type", "discogs"),
    }

    # Use haiku for speed; fall back to channel model if unavailable
    allowed = auth_service.get_allowed_models(user)
    ai_model = channel.get("ai_model", "claude-sonnet")
    feedback_model = "claude-haiku" if "claude-haiku" in allowed else ai_model

    # Build compact collection summary for context (discogs channels only)
    collection_summary = ""
    if channel_context["source_type"] == "discogs":
        try:
            collection_data = await asyncio.to_thread(_get_user_collection, user)
            if collection_data:
                analyzer = _get_analyzer(collection_data)
                profile = analyzer.get_profile()
                collection_summary = radio._build_profile_summary(
                    profile, collection_data, compact=True)
        except Exception:
            pass

    # Convert pydantic models to dicts for the service
    liked = [s.model_dump() for s in data.session_liked]
    disliked = [s.model_dump() for s in data.session_disliked]
    queue_songs = [s.model_dump() for s in data.current_queue]

    try:
        playlist = await asyncio.to_thread(
            radio.generate_replacements,
            session_liked=liked,
            session_disliked=disliked,
            current_queue=queue_songs,
            channel_context=channel_context,
            collection_summary=collection_summary,
            num_songs=data.num_replacements,
            ai_model=feedback_model,
        )
    except Exception as e:
        logger.warning("Feedback generation failed: %s", e)
        return {"songs": [], "replaced": 0}

    if not playlist:
        return {"songs": [], "replaced": 0}

    # Resolve YouTube IDs
    try:
        resolved = await asyncio.to_thread(radio.resolve_youtube_ids, playlist)
    except Exception:
        resolved = playlist  # return without YouTube if resolution fails

    if resolved:
        thumbs.save_recommendations(resolved, source="radio-feedback", data_dir=user_dir)

    return {"songs": resolved, "replaced": len(resolved)}


# ---------------------------------------------------------------------------
# Channel Management
# ---------------------------------------------------------------------------

@app.get("/api/radio/channels")
async def list_channels(request: Request):
    """List all radio channels."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    channels = channel_service.load_channels(data_dir=user_dir)
    return {"channels": channels, "spotify_enabled": True}


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

    # Parse era string into year range
    era_from, era_to = _parse_era(data.era)

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
                ai_model=data.ai_model,
                era_from=era_from,
                era_to=era_to,
                num_songs=data.num_songs,
                data_dir=user_dir,
            )
            return {"status": "ok", "channel": channel}
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

    # Spotify channel
    if not data.spotify_url:
        return JSONResponse(status_code=400, content={"error": "Spotify URL is required"})

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
            ai_model=data.ai_model,
            era_from=era_from,
            era_to=era_to,
            num_songs=data.num_songs,
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


@app.put("/api/radio/channels/{channel_id}/num-songs")
async def update_channel_num_songs_endpoint(channel_id: str, request: Request):
    """Update a channel's playlist size."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelNumSongsRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.update_channel_num_songs(channel_id, data.num_songs,
                                                            data_dir=user_dir)
        cache.invalidate(f"radio_playlist:{user['id']}:{channel_id}")
        return {"status": "ok", "channel": channel}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/ollama/status")
async def ollama_status():
    """Check if Ollama is running and list available models."""
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

    try:
        body = await request.json()
        data = SpotifyPreviewRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    playlist_id = SpotifyService.parse_playlist_url(data.url)
    if not playlist_id:
        return JSONResponse(status_code=400, content={"error": "Invalid Spotify playlist URL"})

    try:
        info = await asyncio.to_thread(spotify.get_playlist_info, playlist_id)
        return info
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Could not fetch playlist. Is it public?"})


# ---------------------------------------------------------------------------
# YouTube Playlist Preview & Channel Creation
# ---------------------------------------------------------------------------

class YouTubePreviewRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=500)


@app.post("/api/radio/youtube-preview")
async def youtube_preview(request: Request):
    """Validate a YouTube URL and return playlist metadata."""
    try:
        body = await request.json()
        data = YouTubePreviewRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    playlist_id = YouTubePlaylistService.parse_playlist_url(data.url)
    if not playlist_id:
        return JSONResponse(status_code=400, content={"error": "Invalid YouTube playlist URL. Use a URL with ?list=..."})

    try:
        info = await asyncio.to_thread(youtube_playlist.get_playlist_info, data.url)
        return info
    except YouTubeServiceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Could not fetch playlist."})


class YouTubeChannelRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=10, max_length=500)
    mode: str = Field("similar_songs", pattern=r"^(play_playlist|similar_songs|new_discoveries)$")
    ai_model: str = Field("ollama", pattern=r"^(claude-sonnet|claude-haiku|ollama)$")
    era: str = Field("", max_length=20)
    num_songs: int = Field(50, ge=5, le=100)


@app.post("/api/radio/youtube-channel")
async def create_youtube_channel(request: Request):
    """Create a channel from a YouTube playlist."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)

    try:
        body = await request.json()
        data = YouTubeChannelRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    playlist_id = YouTubePlaylistService.parse_playlist_url(data.url)
    if not playlist_id:
        return JSONResponse(status_code=400, content={"error": "Invalid YouTube playlist URL"})

    try:
        tracks = await asyncio.to_thread(youtube_playlist.get_playlist_tracks, data.url)
    except YouTubeServiceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to fetch playlist."})

    if not tracks:
        return JSONResponse(status_code=400, content={"error": "Playlist is empty or private."})

    era_from, era_to = _parse_era(data.era)

    try:
        channel = channel_service.create_channel(
            name=data.name,
            source_type="youtube",
            source_data={
                "playlist_id": playlist_id,
                "url": data.url,
                "tracks": tracks,
                "track_count": len(tracks),
            },
            mode=data.mode,
            ai_model=data.ai_model,
            era_from=era_from,
            era_to=era_to,
            num_songs=data.num_songs,
            data_dir=user_dir,
        )
        return {"status": "ok", "channel": channel, "track_count": len(tracks)}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ---------------------------------------------------------------------------
# Upload Channel
# ---------------------------------------------------------------------------

@app.post("/api/radio/upload-channel")
async def create_upload_channel(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=100),
    mode: str = Form(..., pattern=r"^(play_playlist|similar_songs|new_discoveries)$"),
    ai_model: str = Form("claude-sonnet"),
    era: str = Form(""),
    num_songs: int = Form(50),
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

    era_from, era_to = _parse_era(era)
    num_songs = max(5, min(100, num_songs))

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
            ai_model=ai_model,
            era_from=era_from,
            era_to=era_to,
            num_songs=num_songs,
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
    ai_model: str = Query("", max_length=20),
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

    # Use the requested model if allowed, otherwise fall back
    allowed = auth_service.get_allowed_models(user)
    if ai_model and ai_model in allowed:
        provider = ai_model
    else:
        provider = "claude-sonnet" if "claude-sonnet" in allowed else (
            "claude-haiku" if "claude-haiku" in allowed else "ollama"
        )

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
            provider=provider,
            max_tokens=500,
            anthropic_api_key=settings.anthropic_api_key,
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
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
# Lyrics & Song Meaning
# ---------------------------------------------------------------------------

@app.get("/api/lyrics")
async def lyrics_endpoint(
    request: Request,
    artist: str = Query(..., min_length=1, max_length=300),
    title: str = Query(..., min_length=1, max_length=300),
    ai_model: str = Query("", max_length=20),
):
    """Fetch synced/plain lyrics from lrclib.net, fall back to AI recall."""
    cache_key = f"lyrics:{artist.lower()}:{title.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    result = None

    # 1) Try lrclib.net (has synced timestamps)
    try:
        resp = await asyncio.to_thread(
            _httpx.get,
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            timeout=8.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            synced = data.get("syncedLyrics") or ""
            plain = data.get("plainLyrics") or ""
            if synced or plain:
                result = {
                    "found": True,
                    "syncedLyrics": synced,
                    "plainLyrics": plain,
                    "instrumental": data.get("instrumental", False),
                    "source": "lrclib",
                }
    except Exception:
        pass

    # 2) Fallback: ask AI model to recall lyrics
    if not result:
        try:
            from services.llm_provider import call_llm as _call_llm
            user = request.state.user
            allowed = auth_service.get_allowed_models(user)
            if ai_model and ai_model in allowed:
                provider = ai_model
            else:
                provider = "claude-sonnet" if "claude-sonnet" in allowed else (
                    "claude-haiku" if "claude-haiku" in allowed else "ollama"
                )

            ai_text = await asyncio.to_thread(
                _call_llm,
                system_prompt=(
                    "You are a lyrics assistant. Reproduce the full lyrics of the requested song "
                    "as accurately as possible. Return ONLY the lyrics text, with blank lines "
                    "between sections/verses. Do not add any commentary, headers, labels, or "
                    "explanations — just the raw lyrics. If you don't know the lyrics or the "
                    "song is instrumental, reply with exactly: [INSTRUMENTAL]"
                ),
                user_prompt=f'"{title}" by {artist}',
                provider=provider,
                max_tokens=2000,
                anthropic_api_key=settings.anthropic_api_key,
                ollama_base_url=settings.ollama_base_url,
                ollama_model=settings.ollama_model,
            )
            ai_text = ai_text.strip()
            if ai_text and ai_text != "[INSTRUMENTAL]":
                result = {
                    "found": True,
                    "syncedLyrics": "",
                    "plainLyrics": ai_text,
                    "instrumental": False,
                    "source": "ai",
                }
            elif ai_text == "[INSTRUMENTAL]":
                result = {
                    "found": True,
                    "syncedLyrics": "",
                    "plainLyrics": "",
                    "instrumental": True,
                    "source": "ai",
                }
        except Exception as e:
            logger.warning("AI lyrics fallback failed for %s - %s: %s", artist, title, e)

    if not result:
        result = {"found": False, "syncedLyrics": "", "plainLyrics": "", "instrumental": False, "source": ""}

    cache.set(cache_key, result, ttl=86400)
    return result


@app.get("/api/song-meaning")
async def song_meaning_endpoint(
    request: Request,
    artist: str = Query(..., min_length=1, max_length=300),
    title: str = Query(..., min_length=1, max_length=300),
    album: str = Query("", max_length=300),
    ai_model: str = Query("", max_length=20),
):
    """AI-generated song interpretation with mood/theme data for dynamic UI theming."""
    user = request.state.user
    cache_key = f"song_meaning:{artist.lower()}:{title.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    from services.llm_provider import call_llm as _call_llm

    # Use the requested model if allowed, otherwise fall back
    allowed = auth_service.get_allowed_models(user)
    if ai_model and ai_model in allowed:
        provider = ai_model
    else:
        provider = "claude-sonnet" if "claude-sonnet" in allowed else (
            "claude-haiku" if "claude-haiku" in allowed else "ollama"
        )

    album_ctx = f' from the album "{album}"' if album else ""

    system_prompt = "You are a music analyst. Return ONLY valid JSON, no other text."
    user_prompt = (
        f'Analyze "{title}" by {artist}{album_ctx}.\n\n'
        f"Return a JSON object with these keys:\n"
        f'- "summary": 2-3 sentence interpretation of what the song is about\n'
        f'- "themes": array of 2-4 emotional/lyrical themes (e.g. "heartbreak", "nostalgia", "rebellion")\n'
        f'- "mood": single word mood (e.g. "melancholic", "euphoric", "aggressive", "dreamy", "energetic")\n'
        f'- "genres": array of 1-3 genre tags\n'
        f'- "artist_context": 1-2 sentences about what the artist has said about this song, or its cultural significance. If unknown, say "No known artist commentary."\n'
        f'- "color_palette": object with "primary" (hex), "secondary" (hex), "accent" (hex) — bright, vivid colors that match the song\'s mood/vibe. These are used as text colors on a dark background, so they MUST be light/bright enough to read (avoid dark or muted colors like #333, #1a1a2e, #2d1b4e — use bright ones like #e8a03e, #64b4ff, #ff6b9d)\n'
        f'- "bg_gradient": CSS gradient string for the player background. MUST use very dark colors (lightness below 25%) so white text remains readable. Example: linear-gradient(135deg, #1a0a0a 0%, #0d1117 50%, #1a0d1e 100%)\n'
        f"\nReturn ONLY the JSON object."
    )

    try:
        text = await asyncio.to_thread(
            _call_llm,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider=provider,
            max_tokens=800,
            anthropic_api_key=settings.anthropic_api_key,
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
        )

        # Parse JSON from response
        text = re.sub(r"```(?:json)?\s*\n?", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            data = json.loads(text)

        result = {
            "found": True,
            "summary": data.get("summary", ""),
            "themes": data.get("themes", []),
            "mood": data.get("mood", ""),
            "genres": data.get("genres", []),
            "artist_context": data.get("artist_context", ""),
            "color_palette": data.get("color_palette", {}),
            "bg_gradient": data.get("bg_gradient", ""),
        }
    except Exception as e:
        logger.warning("Song meaning failed for %s - %s: %s", artist, title, e)
        result = {"found": False, "summary": "", "themes": [], "mood": "", "genres": [],
                  "artist_context": "", "color_palette": {}, "bg_gradient": ""}

    cache.set(cache_key, result, ttl=86400)
    return result


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
        url = body.get("url", "")
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
