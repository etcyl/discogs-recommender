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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx as _httpx
from fastapi import FastAPI, Request, Query, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import (HTMLResponse, JSONResponse, StreamingResponse,
                               RedirectResponse, Response)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from config import settings
from services.cache import cache
from services.discogs_service import DiscogsService
from services.recommendation import CollectionAnalyzer
from services.claude_recommender import ClaudeRecommender
from services.radio_service import RadioService
from services.llm_provider import LLMError, parse_llm_json
from services import paths
from services import thumbs
from services import channel_service
from services import verification
from services import guardrails
from services import audit
from services import network
from services import passwords
from services import household
from services import playback_log
from services.database import init_db
from services import auth_service
from services.scene_service import SceneService
from services.preference_service import PreferenceService
from services.credit_service import CreditService

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
# utf-8-sig, not utf-8: an editor (or PowerShell's Out-File) can leave a BOM
# on this file, and the BOM then renders as mojibake in the page footer.
APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8-sig").strip()

app = FastAPI(title="Discogs Recommender", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Trusted host middleware (CWE-346) — allow localhost + any *.trycloudflare.com tunnel
_ALLOWED_HOSTS = [
    "localhost", "127.0.0.1", "*.trycloudflare.com",
]
if settings.lan_access:
    # Reached by IP from other devices on the network, and often by the
    # machine's own name. The Host header is not an authentication mechanism —
    # it only stops rebinding-style attacks — so widening it here is safe;
    # who may actually sign in is decided by the auth middleware.
    from services import network as _network
    _ALLOWED_HOSTS += _network.lan_addresses()
    _ALLOWED_HOSTS += ["*.local", "*.lan", "*.home", "*.internal"]
    _ALLOWED_HOSTS += _network.local_hostnames()
_ALLOWED_HOSTS += settings.allowed_host_list
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
    prompt_tier=settings.prompt_tier,
)
scene_service = SceneService()
preference_service = PreferenceService()

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
audit.init_audit_db()
playback_log.init_playback_db()
if settings.audit_enabled:
    audit.prune(settings.audit_retention_days)
    playback_log.prune(settings.audit_retention_days)
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
    match_attributes: list[str] = Field(default_factory=list)
    match_score: int | None = Field(None, ge=0, le=100)

    @field_validator("genres", "styles", "match_attributes")
    @classmethod
    def validate_lists(cls, v: list[str]) -> list[str]:
        return [s[:200] for s in v[:20] if isinstance(s, str)]


class SkipRequest(BaseModel):
    match_attributes: list[str] = Field(default_factory=list)

    @field_validator("match_attributes")
    @classmethod
    def validate_attrs(cls, v: list[str]) -> list[str]:
        return [s[:200] for s in v[:10] if isinstance(s, str)]


class ChannelDeepCutsRequest(BaseModel):
    prefer_deep_cuts: bool = Field(...)


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

    # Caching.
    #
    # Static files already carry an ETag but had no Cache-Control at all, and
    # a browser with no directive falls back to *heuristic* caching — roughly
    # a tenth of the file's age, so an hour-old script can be served stale for
    # six minutes and a week-old one for most of a day. That turns "reload the
    # page" into "hold ctrl and shift while you reload the page", which is not
    # something to ask a household. `no-cache` still caches; it just requires
    # a revalidation, which the ETag answers with a cheap 304.
    #
    # Everything else is per-user and authenticated, so it must not be stored
    # by any shared cache that might hand one person another's page.
    path = request.url.path
    if path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    else:
        response.headers.setdefault("Cache-Control", "no-store, private")

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net https://www.youtube.com; "
        "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
        # Album art is enriched from iTunes and Deezer (see
        # RadioService._fetch_song_metadata), so their CDNs have to be allowed
        # or every cover silently fails to load and leaves a broken image box.
        "img-src 'self' data: https://i.discogs.com https://i.ytimg.com "
        "https://*.ytimg.com https://*.mzstatic.com https://*.dzcdn.net; "
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


def _client_ip(request: Request) -> str:
    return network.client_ip(request, trust_proxy=settings.trust_proxy_headers)


def _is_local_request(request: Request) -> bool:
    """True only for requests originating on this machine.

    A forwarded header means the request passed through something else, so
    the connection address is no longer proof of origin — unless the operator
    has told us the proxy is theirs.
    """
    if not settings.trust_proxy_headers and (
            request.headers.get("x-forwarded-for")
            or request.headers.get("x-real-ip")
            or request.headers.get("cf-connecting-ip")):
        return False
    return network.is_loopback(_client_ip(request))


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookie should carry the Secure flag.

    True on HTTPS. On plain HTTP a Secure cookie is discarded by the browser
    (localhost excepted), which over a home network looks like a login that
    silently does nothing.
    """
    if request.url.scheme == "https":
        return True
    if settings.trust_proxy_headers and \
            request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Attach the signed-in user to the request, or send them to /login.

    Order matters. A real session is checked *first*, before the single-user
    convenience: with the old order, every visitor was silently handed the
    admin account and a second person could never be themselves — their
    session cookie was ignored.

    The convenience is also now restricted to this machine. It exists so the
    owner isn't asked to log into their own laptop; extending it to the
    network would hand admin to anyone who can reach the port.
    """
    path = request.url.path

    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        request.state.user = None
        return await call_next(request)

    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id:
        user = auth_service.validate_session(session_id)
        if user:
            request.state.user = user
            return await call_next(request)

    if settings.single_user_mode and _is_local_request(request):
        admin = auth_service.get_admin_user()
        if admin:
            request.state.user = admin
            return await call_next(request)

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


# Raw upstream errors are accurate but useless to a user — "401: Invalid
# consumer token" tells them nothing about what to do. Map the ones we
# actually see onto an instruction.
_FRIENDLY_ERRORS = (
    ("invalid consumer token",
     "Discogs rejected the API token. Check DISCOGS_TOKEN in your .env — "
     "generate a new one at discogs.com/settings/developers."),
    ("401",
     "Discogs rejected the credentials. Check DISCOGS_TOKEN and "
     "DISCOGS_USERNAME in your .env."),
    ("403",
     "Discogs refused the request. Your token may lack permission for this "
     "collection, or the collection is private."),
    ("404",
     "Discogs could not find that — check DISCOGS_USERNAME in your .env."),
    ("429",
     "Discogs rate limit reached (60 requests/minute). Wait a minute and "
     "try again."),
    ("cannot connect to ollama",
     "Ollama isn't reachable. Start it with `ollama serve`, or switch the "
     "channel's AI model."),
)


def _sanitize_error(error: Exception) -> str:
    """Return a safe, actionable error message without leaking internals (CWE-209)."""
    msg = str(error)

    sensitive_patterns = []
    if settings.discogs_token:
        sensitive_patterns.append(settings.discogs_token)
    if settings.anthropic_api_key:
        sensitive_patterns.append(settings.anthropic_api_key)
    for pattern in sensitive_patterns:
        if pattern in msg:
            msg = msg.replace(pattern, "[REDACTED]")

    lowered = msg.lower()
    for needle, friendly in _FRIENDLY_ERRORS:
        if needle in lowered:
            logger.warning("Upstream error surfaced to user: %s", msg)
            return friendly

    return msg


def _get_user_data_dir(user: dict) -> Path:
    """Return the per-user data directory, creating it if needed."""
    user_dir = paths.data_dir() / user["id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _get_user_discogs(user: dict) -> DiscogsService | None:
    """Return a DiscogsService for this user, or None if they have no Discogs.

    Only the admin falls back to the server-configured account. Everyone else
    gets their own or nothing — otherwise a second person on the network would
    silently browse and get recommendations from the owner's record collection.
    """
    if user.get("discogs_username"):
        cache_key = f"discogs_service:{user['id']}"
        svc = cache.get(cache_key)
        if not svc:
            svc = DiscogsService(settings.app_name,
                                 user.get("discogs_token") or "",
                                 user["discogs_username"])
            cache.set(cache_key, svc, ttl=3600)
        return svc
    if user.get("is_admin"):
        return discogs  # May be None if no Discogs configured
    return None


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
    """The name to greet this user by.

    Only the admin falls back to the server-configured Discogs username.
    Without that restriction a second account with no Discogs of its own was
    greeted by the owner's name — Bee signing in and being welcomed as etcyl.
    """
    username = user.get("discogs_username")
    if not username and user.get("is_admin"):
        username = settings.discogs_username
    if username and username != "local":
        return username
    return user.get("display_name") or user.get("login_name") or "there"


def _user_has_discogs(user: dict) -> bool:
    """Whether *this* user has a Discogs collection to work from.

    Not the same question as settings.discogs_configured, which describes the
    server. A guest on the home network has no Discogs of their own, and
    handing them a collection-backed default channel just produces an error
    they can do nothing about.
    """
    if user.get("discogs_username"):
        return True
    return bool(user.get("is_admin") and settings.discogs_configured)


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
    """Sign-in form. The owner is signed in automatically on this machine."""
    session_id = request.cookies.get(auth_service.COOKIE_NAME)
    if session_id and auth_service.validate_session(session_id):
        return RedirectResponse(url="/", status_code=302)

    if settings.single_user_mode and _is_local_request(request):
        admin = auth_service.get_admin_user()
        if admin:
            new_session = auth_service.create_session(admin["id"])
            response = RedirectResponse(url="/", status_code=302)
            auth_service.set_session_cookie(response, new_session,
                                            secure=_cookie_secure(request))
            return response

    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "app_version": APP_VERSION,
        "next": _safe_next(request.query_params.get("next", "")),
    })


def _safe_next(target: str) -> str:
    """Only allow redirects to a path on this site (CWE-601).

    A bare "/path" is fine; anything with a scheme or host, or the
    protocol-relative "//evil.example", is not.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return ""
    return target[:200]


@app.post("/login")
async def login_submit(request: Request):
    """Verify a username and password, then start a session."""
    ip = _client_ip(request) or "unknown"
    if _is_rate_limited(f"login:{ip}", max_requests=8, window_seconds=60):
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "app_version": APP_VERSION,
            "error": "Too many attempts from this device. Wait a minute and try again.",
        }, status_code=429)

    form = await request.form()
    login_name = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    target = _safe_next(str(form.get("next", "")))

    try:
        user = auth_service.authenticate(login_name, password)
    except auth_service.AuthError as e:
        logger.info("Failed sign-in for %r from %s", login_name[:40], ip)
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "app_version": APP_VERSION,
            "error": str(e), "username": login_name[:64],
            "next": target,
        }, status_code=401)

    session_id = auth_service.create_session(user["id"])
    destination = "/account/password" if user.get("must_change_password") else (target or "/")
    response = RedirectResponse(url=destination, status_code=302)
    auth_service.set_session_cookie(response, session_id,
                                    secure=_cookie_secure(request))
    logger.info("Signed in: %s from %s", user.get("login_name") or user["id"], ip)
    return response


@app.get("/account/password", response_class=HTMLResponse)
async def password_page(request: Request):
    """Let the signed-in user change their own password."""
    user = request.state.user
    return templates.TemplateResponse(request, "password.html", _template_context(
        request, must_change=bool(user.get("must_change_password")),
        has_password=bool(user.get("password_hash")), active_page="account"))


@app.post("/account/password")
async def password_change(request: Request):
    user = request.state.user
    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))

    def fail(message: str):
        return templates.TemplateResponse(request, "password.html", _template_context(
            request, error=message,
            must_change=bool(user.get("must_change_password")),
            has_password=bool(user.get("password_hash")),
            active_page="account"), status_code=400)

    if new != confirm:
        return fail("The two new passwords don't match.")

    try:
        if user.get("password_hash"):
            auth_service.change_password(user["id"], current, new)
        else:
            # Admin accounts created before passwords existed have none yet.
            auth_service.set_password(user["id"], new, revoke_sessions=False)
    except (auth_service.AuthError, passwords.PasswordError) as e:
        return fail(str(e))

    return templates.TemplateResponse(request, "password.html", _template_context(
        request, saved=True, must_change=False, has_password=True,
        active_page="account"))


@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    """Show invite acceptance / setup page."""
    invite = auth_service.get_invite(token)
    if not invite:
        return templates.TemplateResponse(request,"login.html", {
            "request": request,
            "error": "This invite link is invalid or has expired.",
        })
    return templates.TemplateResponse(request,"setup.html", {
        "request": request, "token": token,
    })


@app.post("/invite/{token}")
async def invite_accept(request: Request, token: str):
    """Process invite acceptance: create user, set session, redirect."""
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(f"invite:{client_ip}", max_requests=5, window_seconds=60):
        return templates.TemplateResponse(request,"login.html", {
            "request": request, "error": "Too many attempts. Please wait a minute.",
        })

    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()[:100]
    discogs_username = str(form.get("discogs_username", "")).strip()[:100]
    discogs_token = str(form.get("discogs_token", "")).strip()[:200]

    if not display_name:
        return templates.TemplateResponse(request,"setup.html", {
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
        return templates.TemplateResponse(request,"setup.html", {
            "request": request, "token": token, "error": str(e),
        })

    session_id = auth_service.create_session(user["id"])
    response = RedirectResponse(url="/", status_code=302)
    auth_service.set_session_cookie(response, session_id,
                                    secure=_cookie_secure(request))
    return response


@app.post("/admin/create-account")
async def admin_create_account(request: Request):
    """Create a username/password account for someone else on the network."""
    user = request.state.user
    if not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})

    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()[:100]
    login_name = str(form.get("login_name", "")).strip()
    password = str(form.get("password", ""))
    models = str(form.get("allowed_models", "ollama")).strip() or "ollama"

    try:
        account = auth_service.create_account(
            display_name=display_name or login_name,
            login_name=login_name,
            password=password,
            allowed_models=models,
        )
    except (auth_service.AuthError, passwords.PasswordError) as e:
        return RedirectResponse(url=f"/admin?error={quote(str(e))}", status_code=302)

    logger.info("Admin created account %s", account.get("login_name"))
    return RedirectResponse(
        url=f"/admin?created={quote(account.get('login_name') or '')}",
        status_code=302)


@app.post("/admin/reset-password")
async def admin_reset_password(request: Request):
    """Set a new password for another account, forcing them to change it."""
    user = request.state.user
    if not user.get("is_admin"):
        return JSONResponse(status_code=403, content={"error": "Admin only"})

    form = await request.form()
    user_id = str(form.get("user_id", "")).strip()
    password = str(form.get("password", ""))

    target = auth_service.get_user(user_id)
    if not target or target.get("is_admin"):
        return RedirectResponse(url="/admin?error=Unknown+account", status_code=302)

    try:
        auth_service.set_password(user_id, password, must_change=True)
    except passwords.PasswordError as e:
        return RedirectResponse(url=f"/admin?error={quote(str(e))}", status_code=302)

    return RedirectResponse(url="/admin?reset=1", status_code=302)


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
    lan_urls = [f"http://{ip}:8000" for ip in network.lan_addresses()]
    return templates.TemplateResponse(request, "admin.html", _template_context(
        request, invites=invites, users=users,
        lan_access=settings.lan_access, lan_urls=lan_urls,
        active_page="admin"))


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
        "discogs_public_mode": settings.discogs_public_mode,
        "anthropic_configured": settings.anthropic_configured,
        "ollama_available": ollama_info["running"],
        "ollama_models": ollama_info["models"],
        "ollama_installed": ollama_info["installed"],
        "verification_policy": settings.verification_policy,
        "audit_enabled": settings.audit_enabled,
    }


@app.get("/api/system/hardware")
async def system_hardware(request: Request):
    """Return hardware info for local model recommendations."""
    from services.hardware_service import get_hardware_info
    info = await asyncio.to_thread(get_hardware_info, settings.ollama_base_url)
    return info


# ---------------------------------------------------------------------------
# Household — see and play what other people here are listening to
# ---------------------------------------------------------------------------

@app.post("/api/now-playing")
async def report_now_playing(request: Request):
    """The player reports the track it just started, for the household view."""
    user = request.state.user
    if not user.get("share_activity", 1):
        return {"shared": False}
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    await asyncio.to_thread(
        household.set_now_playing,
        user["id"],
        guardrails.sanitize(str(body.get("artist", "")), 300),
        guardrails.sanitize(str(body.get("title", "")), 300),
        guardrails.sanitize(str(body.get("album", "")), 300),
        re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("videoId", "")))[:32],
        guardrails.sanitize(str(body.get("channel", "")), 100),
    )
    return {"shared": True}


@app.post("/api/playback/event")
async def record_playback_event(request: Request):
    """The player reports a track that failed or needed a fallback video."""
    user = request.state.user
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    event = str(body.get("event", ""))
    if event not in ("error", "recovered", "unavailable"):
        return JSONResponse(status_code=400, content={"error": "Unknown event"})

    try:
        code = int(body.get("errorCode"))
    except (TypeError, ValueError):
        code = None

    await asyncio.to_thread(
        playback_log.record,
        user_id=user["id"], event=event,
        artist=guardrails.sanitize(str(body.get("artist", "")), 300),
        title=guardrails.sanitize(str(body.get("title", "")), 300),
        video_id=re.sub(r"[^A-Za-z0-9_-]", "", str(body.get("videoId", "")))[:32],
        error_code=code,
        channel_id=re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("channelId", "")))[:64],
        channel_name=guardrails.sanitize(str(body.get("channel", "")), 100),
        detail=guardrails.sanitize(str(body.get("detail", "")), 300))
    return {"recorded": True}


@app.get("/api/playback/problems")
async def playback_problems(request: Request):
    """Which tracks are failing, for this account or (admin) everyone."""
    user = request.state.user
    scope_all = bool(user.get("is_admin")) and \
        request.query_params.get("all") == "1"
    scope = None if scope_all else user["id"]
    data = await asyncio.to_thread(playback_log.summary, scope)
    data["recent"] = await asyncio.to_thread(playback_log.recent, scope, 100)
    return data


@app.get("/api/household")
async def household_status(request: Request):
    """Who else is here and what they're playing."""
    people = await asyncio.to_thread(household.household, request.state.user)
    return {"people": people,
            "sharing": bool(request.state.user.get("share_activity", 1))}


@app.get("/household", response_class=HTMLResponse)
async def household_page(request: Request):
    user = request.state.user
    people = await asyncio.to_thread(household.household, user)
    return templates.TemplateResponse(request, "household.html", _template_context(
        request, people=people,
        sharing=bool(user.get("share_activity", 1)),
        active_page="household"))


@app.get("/household/{user_id}/likes", response_class=HTMLResponse)
async def household_likes(request: Request, user_id: str):
    """Someone else's liked songs, read-only."""
    user = request.state.user
    if not re.match(r"^[a-zA-Z0-9]+$", user_id):
        return RedirectResponse(url="/household", status_code=302)
    if not household.can_view(user, user_id):
        return templates.TemplateResponse(request, "household.html", _template_context(
            request, people=[], sharing=bool(user.get("share_activity", 1)),
            error="That list isn't shared with you.",
            active_page="household"), status_code=403)

    songs = await asyncio.to_thread(household.liked_songs, user_id)
    return templates.TemplateResponse(request, "household_likes.html", _template_context(
        request, songs=songs, owner_id=user_id,
        owner_name=household.display_name(user_id),
        is_self=(user_id == user["id"]),
        active_page="household"))


@app.post("/household/{user_id}/play-likes")
async def play_household_likes(request: Request, user_id: str):
    """Copy someone's liked songs into a channel of your own and play it.

    A copy, not a live mirror: their list keeps changing as they listen, and a
    playlist that reshuffles under you while it plays is worse than one you
    chose. Re-run it to pick up their newer likes.
    """
    user = request.state.user
    if not re.match(r"^[a-zA-Z0-9]+$", user_id) or not household.can_view(user, user_id):
        return JSONResponse(status_code=403, content={"error": "Not shared with you"})

    songs = await asyncio.to_thread(household.liked_songs, user_id)
    if not songs:
        return JSONResponse(status_code=400,
                            content={"error": "They haven't liked anything yet."})

    owner_name = household.display_name(user_id)
    tracks = [{
        "artist": s.get("artist", ""), "title": s.get("title", ""),
        "album": s.get("album", ""), "year": "",
        "reason": f"Liked by {owner_name}",
    } for s in songs if s.get("artist") and s.get("title")]

    user_dir = _get_user_data_dir(user)
    name = f"{owner_name}'s Likes"
    for ch in channel_service.load_channels(data_dir=user_dir):
        if ch.get("name") == name and not ch.get("is_default"):
            try:
                channel_service.delete_channel(ch["id"], data_dir=user_dir)
            except ValueError:
                pass

    channel = await asyncio.to_thread(
        channel_service.create_channel,
        name=name, source_type="upload", source_data={"tracks": tracks},
        mode="play_playlist", num_songs=min(100, len(tracks)),
        data_dir=user_dir)

    cache.invalidate(f"radio_playlist:{user['id']}:{channel['id']}")
    return {"channel_id": channel["id"], "name": channel["name"],
            "tracks": len(tracks)}


@app.post("/account/sharing")
async def update_sharing(request: Request):
    """Turn household sharing on or off for the signed-in account."""
    user = request.state.user
    form = await request.form()
    enabled = str(form.get("share_activity", "")).lower() in ("1", "true", "on", "yes")
    await asyncio.to_thread(household.set_sharing, user["id"], enabled)
    return RedirectResponse(url="/account/password?sharing=1", status_code=302)


# ---------------------------------------------------------------------------
# Transparency / audit
#
# Every AI generation is recorded. These routes make that record readable —
# a listener can see which model produced a playlist, what it claimed, and
# whether anything independently backed it up. Scoped to the owner of the
# runs; admins see everything.
# ---------------------------------------------------------------------------

@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    """Human-readable log of AI generations."""
    user = request.state.user
    scope_all = bool(user.get("is_admin")) and request.query_params.get("all") == "1"
    runs = await asyncio.to_thread(
        audit.list_runs, None if scope_all else user["id"], 100)
    model_stats = await asyncio.to_thread(
        audit.stats, None if scope_all else user["id"])
    playback = await asyncio.to_thread(
        playback_log.summary, None if scope_all else user["id"])
    return templates.TemplateResponse(request, "audit.html", _template_context(
        request, runs=runs, model_stats=model_stats["by_model"],
        playback=playback, scope_all=scope_all,
        verification_policy=settings.verification_policy,
        audit_enabled=settings.audit_enabled,
        retention_days=settings.audit_retention_days,
        active_page="audit"))


@app.get("/api/audit/runs")
async def audit_runs(request: Request,
                     limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0)):
    user = request.state.user
    scope_all = bool(user.get("is_admin")) and request.query_params.get("all") == "1"
    runs = await asyncio.to_thread(
        audit.list_runs, None if scope_all else user["id"], limit, offset)
    return {"runs": runs}


@app.get("/api/audit/runs/{run_id}")
async def audit_run_detail(request: Request, run_id: int):
    """One run with every song, including the ones the accuracy check removed."""
    user = request.state.user
    scope = None if user.get("is_admin") else user["id"]
    run = await asyncio.to_thread(audit.get_run, run_id, scope)
    if not run:
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    return run


@app.get("/api/audit/export/{run_id}")
async def audit_export(request: Request, run_id: int):
    """Download one run as JSON, for review outside this app."""
    user = request.state.user
    scope = None if user.get("is_admin") else user["id"]
    payload = await asyncio.to_thread(audit.export_run, run_id, scope)
    if payload == "{}":
        return JSONResponse(status_code=404, content={"error": "Run not found"})
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="generation-run-{run_id}.json"'})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Dashboard with collection profile summary."""
    user = request.state.user
    profile = None
    error = None
    discogs_configured = _user_has_discogs(user)

    if discogs_configured:
        try:
            collection = await asyncio.to_thread(_get_user_collection, user)
            if collection:
                analyzer = _get_analyzer(collection)
                profile = analyzer.get_profile()
        except Exception as e:
            error = _sanitize_error(e)

    return templates.TemplateResponse(request,"index.html",
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

    return templates.TemplateResponse(request,"collection.html",
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
    return templates.TemplateResponse(request,"recommendations.html",
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

    return templates.TemplateResponse(request,"search.html",
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

    return templates.TemplateResponse(request,"release.html",
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
    has_discogs = _user_has_discogs(user)
    channels = channel_service.load_channels(
        data_dir=user_dir, discogs_configured=has_discogs)
    allowed_models = list(auth_service.get_allowed_models(user))
    return templates.TemplateResponse(request,"radio.html",
                                      _template_context(request, channels=channels,
                                                        allowed_models=allowed_models,
                                                        discogs_configured=has_discogs))


@app.get("/api/radio/playlist")
async def radio_playlist(request: Request,
                         channel_id: str = Query("my-collection")):
    """Generate a radio playlist with YouTube video IDs."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    _ch = channel_service.get_channel(channel_id, data_dir=user_dir)
    # A fixed playlist (liked songs, or any play_playlist channel) is an
    # explicit "play these tracks" and is not filtered against history.
    _is_liked = bool(_ch) and (_ch.get("source_type") == "liked"
                               or _ch.get("mode") == "play_playlist")

    cache_key = f"radio_playlist:{user['id']}:{channel_id}"
    playlist = cache.get(cache_key)
    if playlist:
        # Post-cache filter: remove disliked songs; for non-liked channels also
        # remove liked/played/rec-history songs
        filter_set = thumbs.get_dislikes_set(data_dir=user_dir)
        if not _is_liked:
            filter_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
            filter_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
            filter_set.update(thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir))
        playlist = [
            s for s in playlist
            if (s.get("artist", "").lower().strip(),
                s.get("title", "").lower().strip()) not in filter_set
            and thumbs.normalize_song_key(
                s.get("artist", ""), s.get("title", "")) not in filter_set
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

        # Look up the channel early: whether history filtering applies at all
        # depends on what kind of channel this is.
        #
        # "Don't replay what you've heard recently" is right for a channel that
        # generates recommendations. It is wrong for one that plays a fixed
        # playlist — a liked-songs channel, or any channel in play_playlist
        # mode. Those are an explicit "play exactly these tracks", and filtering
        # them against listening history empties the channel the second time
        # you open it.
        _ch_for_cache = channel_service.get_channel(channel_id, data_dir=user_dir)
        _is_fixed_playlist = bool(_ch_for_cache) and (
            _ch_for_cache.get("source_type") == "liked"
            or _ch_for_cache.get("mode") == "play_playlist")

        cache_key = f"radio_playlist:{user['id']}:{channel_id}"
        playlist = cache.get(cache_key)
        if playlist:
            # Disliked songs are always removed — that preference holds even
            # for a hand-picked playlist.
            filter_set = thumbs.get_dislikes_set(data_dir=user_dir)
            if not _is_fixed_playlist:
                filter_set.update(thumbs.get_thumbs_set(data_dir=user_dir))
                filter_set.update(thumbs.get_history_set(max_entries=300, data_dir=user_dir))
                filter_set.update(thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir))
            playlist = [
                s for s in playlist
                if (s.get("artist", "").lower().strip(),
                    s.get("title", "").lower().strip()) not in filter_set
                and thumbs.normalize_song_key(
                    s.get("artist", ""), s.get("title", "")) not in filter_set
            ]
            yield _sse("song", {"songs": playlist, "total_expected": len(playlist)})
            yield _sse("complete", {"cached": True, "ai_model": ""})
            return

        _t_start = time.time()
        guardrail_findings: list[str] = []

        try:
            channel = channel_service.get_channel(channel_id, data_dir=user_dir)
            if not channel:
                yield _sse("error", {"message": "Channel not found"})
                return

            source_type = channel.get("source_type", "discogs")
            ai_model = channel.get("ai_model", "claude-sonnet")
            num_songs = channel.get("num_songs", 50)
            model_label = AI_MODEL_LABELS.get(ai_model, ai_model)

            # A channel that plays a fixed list never calls a model, so model
            # access is not a question worth asking. It used to be asked
            # anyway, which made every playlist an account had unplayable if
            # the channel's stored ai_model happened to be one they weren't
            # allowed — the model it would never have used.
            needs_model = not _is_fixed_playlist

            if needs_model:
                allowed = auth_service.get_allowed_models(user)
                if ai_model not in allowed:
                    # Fall back to a model they can use rather than leaving a
                    # dead channel. The substitution is announced here and
                    # recorded in the audit log, so it isn't a silent swap.
                    fallback = next(
                        (m for m in ("ollama", "claude-haiku", "claude-sonnet")
                         if m in allowed), None)
                    if not fallback:
                        yield _sse("error", {"message":
                            "No AI model is available to your account. Ask the "
                            "owner to enable one, or use a playlist channel."})
                        return
                    logger.info("Channel %s asks for %s which %s cannot use; "
                                "using %s instead", channel_id, ai_model,
                                user.get("login_name") or user["id"], fallback)
                    ai_model = fallback
                    model_label = AI_MODEL_LABELS.get(ai_model, ai_model)
                    yield _sse("progress", {
                        "message": f"Using {model_label} for this channel.",
                        "percent": 3})

            if source_type == "discogs":
                if not settings.discogs_configured and not user.get("discogs_username"):
                    yield _sse("error", {"message": "No Discogs account connected. Set DISCOGS_USERNAME in your .env — a public collection needs no token — or create a Spotify/YouTube/themed channel instead."})
                    return
                yield _sse("progress", {"message": "Loading your collection from Discogs...", "percent": 5})
                _task = asyncio.ensure_future(asyncio.to_thread(_get_user_collection, user))
                async for _p in _keepalive_loop(_task):
                    yield _p
                collection_data = _task.result()
                if not collection_data:
                    yield _sse("error", {"message": "Collection is empty."})
                    return

                yield _sse("progress", {"message": "Analyzing your taste profile...", "percent": 10})
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
                theme = guardrails.sanitize(
                    channel.get("source_data", {}).get("theme", ""),
                    guardrails.MAX_THEME)
                for _f in guardrails.scan(theme):
                    if _f not in guardrail_findings:
                        guardrail_findings.append(_f)
                prefer_deep_cuts = channel.get("prefer_deep_cuts", False)

                # Build enrichment data (credit graph, scenes, labels, preferences)
                credits_summary = ""
                scene_summary_text = ""
                label_tree_summary = ""
                pref_summary = ""

                is_sonnet = ai_model not in ("ollama", "claude-haiku")

                if is_sonnet:
                    # Credit graph (Discogs-only, cached after first fetch)
                    try:
                        yield _sse("progress", {"message": "Mapping credit connections...", "percent": 12})
                        user_discogs = _get_user_discogs(user)
                        if user_discogs:
                            credit_svc = CreditService(user_discogs)
                            credits_summary = await asyncio.to_thread(
                                credit_svc.get_credit_summary,
                                collection_data, data_dir=user_dir)
                    except Exception as e:
                        logger.warning("Credit fetch failed (non-fatal): %s", e)

                    # Label genealogy (Discogs API, cached 30 days)
                    try:
                        user_discogs = _get_user_discogs(user)
                        if user_discogs and profile.get("top_labels"):
                            yield _sse("progress", {"message": "Building label family tree...", "percent": 16})
                            top_label_names = [l for l, _ in profile["top_labels"][:15]]
                            label_families = await asyncio.to_thread(
                                scene_service.build_label_tree,
                                top_label_names, user_discogs, data_dir=user_dir)
                            label_tree_summary = scene_service.get_label_tree_for_prompt(label_families)
                    except Exception as e:
                        logger.warning("Label tree build failed (non-fatal): %s", e)

                # Scene clustering (no API calls, pure computation)
                try:
                    yield _sse("progress", {"message": "Mapping collection scenes...", "percent": 18})
                    scenes = scene_service.cluster_into_scenes(collection_data)
                    scene_summary_text = scene_service.get_scene_summary_for_prompt(scenes)
                except Exception as e:
                    logger.warning("Scene clustering failed (non-fatal): %s", e)

                # Preference profile (file read)
                try:
                    pref_summary = preference_service.get_preference_summary_for_prompt(
                        data_dir=user_dir)
                except Exception:
                    pass

                try:
                    pq = queue_mod.Queue()
                    def _on_batch(collected, total):
                        pct = 25 + int(5 * collected / max(total, 1))
                        pq.put(_sse("progress", {"message": f"{model_label}: {collected}/{total} songs generated...", "percent": pct}))

                    enrichment_kwargs = dict(
                        credits_summary=credits_summary,
                        scene_summary=scene_summary_text,
                        label_tree_summary=label_tree_summary,
                        preference_summary=pref_summary,
                        prefer_deep_cuts=prefer_deep_cuts,
                    )

                    if theme:
                        yield _sse("progress", {"message": f"{model_label} is curating \"{theme}\" songs...", "percent": 25})
                        coro = asyncio.to_thread(
                            radio.generate_themed_playlist, profile, collection_data,
                            theme, thumbs_summary, dislikes_summary, play_history_summary,
                            exclude_set=exclude_set,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs, on_batch=_on_batch,
                            **enrichment_kwargs)
                    else:
                        yield _sse("progress", {"message": f"{model_label} is curating {num_songs} songs for you...", "percent": 25})
                        coro = asyncio.to_thread(
                            radio.generate_playlist, profile, collection_data, thumbs_summary,
                            dislikes_summary, play_history_summary,
                            exclude_set=exclude_set,
                            discovery=discovery, era_from=era_from, era_to=era_to,
                            ai_model=ai_model, num_songs=num_songs, on_batch=_on_batch,
                            **enrichment_kwargs)
                    _task = asyncio.ensure_future(coro)
                    async for _p in _keepalive_loop(_task, progress_q=pq):
                        yield _p
                    playlist = _task.result()

                    # Post-generation preference reranking
                    if pref_summary:
                        try:
                            playlist = radio._rerank_by_preferences(
                                playlist, preference_service, data_dir=user_dir)
                        except Exception:
                            pass
                except LLMError as e:
                    yield _sse("error", {"message": _sanitize_error(e)})
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
                tracks, _f = guardrails.sanitize_tracks(tracks)
                guardrail_findings.extend(x for x in _f if x not in guardrail_findings)
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
                    sp_deep_cuts = channel.get("prefer_deep_cuts", False)
                    sp_pref_summary = preference_service.get_preference_summary_for_prompt(
                        data_dir=user_dir)
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
                            preference_summary=sp_pref_summary,
                            prefer_deep_cuts=sp_deep_cuts,
                            on_batch=_on_batch_sp))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
                        if sp_pref_summary:
                            try:
                                playlist = radio._rerank_by_preferences(
                                    playlist, preference_service, data_dir=user_dir)
                            except Exception:
                                pass
                    except LLMError as e:
                        yield _sse("error", {"message": _sanitize_error(e)})
                        return

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            elif source_type == "upload":
                tracks = channel.get("source_data", {}).get("tracks", [])
                tracks, _f = guardrails.sanitize_tracks(tracks)
                guardrail_findings.extend(x for x in _f if x not in guardrail_findings)
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
                            "reason": t.get("reason", ""),
                            "similar_to": [],
                            # Carry through anything already resolved, so a
                            # pre-built playlist starts playing immediately
                            # instead of re-searching YouTube for every track.
                            "videoId": t.get("videoId", ""),
                            "altVideoIds": t.get("altVideoIds", []),
                            "thumbnail": t.get("thumbnail", ""),
                            "albumArt": t.get("albumArt", ""),
                            "duration": t.get("duration", ""),
                            "verification": t.get("verification", {}),
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
                    up_deep_cuts = channel.get("prefer_deep_cuts", False)
                    up_pref_summary = preference_service.get_preference_summary_for_prompt(
                        data_dir=user_dir)
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
                            preference_summary=up_pref_summary,
                            prefer_deep_cuts=up_deep_cuts,
                            on_batch=_on_batch_up))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
                        if up_pref_summary:
                            try:
                                playlist = radio._rerank_by_preferences(
                                    playlist, preference_service, data_dir=user_dir)
                            except Exception:
                                pass
                    except LLMError as e:
                        yield _sse("error", {"message": _sanitize_error(e)})
                        return

                if not playlist:
                    yield _sse("error", {"message": "No songs generated."})
                    return

            elif source_type == "youtube":
                tracks = channel.get("source_data", {}).get("tracks", [])
                tracks, _f = guardrails.sanitize_tracks(tracks)
                guardrail_findings.extend(x for x in _f if x not in guardrail_findings)
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
                    yt_deep_cuts = channel.get("prefer_deep_cuts", False)
                    yt_pref_summary = preference_service.get_preference_summary_for_prompt(
                        data_dir=user_dir)
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
                            preference_summary=yt_pref_summary,
                            prefer_deep_cuts=yt_deep_cuts,
                            on_batch=_on_batch_yt))
                        async for _p in _keepalive_loop(_task, progress_q=pq):
                            yield _p
                        playlist = _task.result()
                        if yt_pref_summary:
                            try:
                                playlist = radio._rerank_by_preferences(
                                    playlist, preference_service, data_dir=user_dir)
                            except Exception:
                                pass
                    except LLMError as e:
                        yield _sse("error", {"message": _sanitize_error(e)})
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

            # ---- Accuracy check ------------------------------------------
            # Models invent songs — measured at 44-100% real depending on the
            # model (bench/verification.md). Resolve each pick against public
            # catalogues before spending YouTube lookups on it, so a fabricated
            # track is either labelled or dropped rather than presented as a
            # real recommendation.
            #
            # Skipped when the tracks aren't model output: liked songs and
            # "play this playlist" are the listener's own real tracks.
            verification_summary = {}
            dropped_songs: list[dict] = []
            ai_generated = source_type != "liked" and channel.get("mode") != "play_playlist"
            policy = settings.verification_policy

            if ai_generated and policy != verification.Policy.OFF.value and playlist:
                yield _sse("progress", {
                    "message": f"Fact-checking {len(playlist)} recommendations...",
                    "percent": 28,
                })
                _before = list(playlist)
                _task = asyncio.ensure_future(asyncio.to_thread(
                    verification.verify_songs, playlist, policy))
                async for _p in _keepalive_loop(_task):
                    yield _p
                playlist, verification_summary = _task.result()

                kept_ids = {id(s) for s in playlist}
                dropped_songs = [s for s in _before if id(s) not in kept_ids]

                if verification_summary.get("dropped"):
                    yield _sse("progress", {
                        "message": (f"Dropped {verification_summary['dropped']} "
                                    "unverifiable songs"),
                        "percent": 29,
                    })
                if not playlist:
                    yield _sse("error", {
                        "message": ("Every recommendation failed the accuracy check. "
                                    "Try a different AI model, or set "
                                    "VERIFICATION_POLICY=flag to see them anyway.")})
                    return

            # Build exclude set for post-YouTube-resolution filtering
            # (YT title rewriting can change artist/title to match known songs).
            # A fixed playlist is exempt — see _is_fixed_playlist above.
            yt_filter_set = thumbs.get_dislikes_set(data_dir=user_dir)
            if not _is_fixed_playlist:
                yt_filter_set.update(thumbs.get_rec_history_set(max_entries=500, data_dir=user_dir))
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
                    # The resolver rewrites artist/title from the YouTube video
                    # title. Where a catalogue already confirmed the track, its
                    # name wins over a video title that may belong to an
                    # interview, a live upload, or an unrelated clip.
                    if verification_summary:
                        verification.reconcile(batch_resolved)
                    resolved.extend(batch_resolved)
                    yield _sse("song", {"songs": batch_resolved, "total_expected": total})

            if resolved:
                thumbs.save_recommendations(resolved, source="radio", data_dir=user_dir)
                ttl = 1800 if source_type == "liked" else 28800
                cache.set(cache_key, resolved, ttl=ttl)

            # ---- Audit ---------------------------------------------------
            # One row per generation, plus one per song including the ones the
            # accuracy check removed, so a dropped recommendation stays on the
            # record instead of disappearing.
            # Songs that survived the accuracy check but had no playable match
            # on YouTube. They never reach the listener either, so they belong
            # in the record alongside the ones verification removed.
            #
            # Compared by object identity: resolve_youtube_ids rewrites
            # artist/title in place from the video title, so comparing names
            # here would miss exactly the songs that were rewritten.
            _resolved_ids = {id(s) for s in resolved}
            unresolved_songs = [s for s in playlist if id(s) not in _resolved_ids]

            run_id = None
            if settings.audit_enabled:
                run_id = await asyncio.to_thread(
                    audit.record_run,
                    user_id=user["id"], channel_id=channel_id,
                    channel_name=channel.get("name", ""),
                    source_type=source_type, mode=channel.get("mode", ""),
                    ai_model=ai_model, prompt_tier=settings.prompt_tier,
                    discovery=channel.get("discovery"),
                    era_from=channel.get("era_from"), era_to=channel.get("era_to"),
                    deep_cuts=channel.get("prefer_deep_cuts", False),
                    num_requested=num_songs,
                    songs_kept=resolved, songs_dropped=dropped_songs,
                    songs_unresolved=unresolved_songs,
                    verification_summary=verification_summary,
                    duration_ms=int((time.time() - _t_start) * 1000),
                    app_version=APP_VERSION,
                    notes=("guardrail findings: " + ",".join(guardrail_findings))
                    if guardrail_findings else "")

            yield _sse("progress", {"message": "Ready!", "percent": 100})
            yield _sse("complete", {
                "cached": False,
                # A fixed playlist was not curated by a model, so don't claim
                # one. The channel still carries an ai_model setting for when
                # it is switched out of play_playlist mode.
                "ai_model": "" if _is_fixed_playlist else model_label,
                # Provenance, so the UI can say where this playlist came from
                # rather than presenting it as anonymous truth.
                "provenance": {
                    "ai_model": "" if _is_fixed_playlist else ai_model,
                    "model_label": "" if _is_fixed_playlist else model_label,
                    "source": "fixed playlist" if _is_fixed_playlist else "generated",
                    "prompt_tier": settings.prompt_tier,
                    "verification": verification_summary or {"policy": "off"},
                    "audit_run_id": run_id,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            })

        except Exception as e:
            # This path used to yield str(e) straight to the browser, which
            # both leaked raw upstream text (CWE-209 — the same redaction the
            # rest of the app does) and showed the user things like
            # "401: Invalid consumer token" with no hint what to do.
            logger.exception("Playlist stream failed for channel %s", channel_id)
            yield _sse("error", {"message": _sanitize_error(e)})

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
            match_attributes=thumb_data.match_attributes,
            match_score=thumb_data.match_score,
            data_dir=user_dir,
        )
        # Record positive signal for preference learning
        if thumb_data.match_attributes:
            try:
                preference_service.record_positive(
                    thumb_data.match_attributes, data_dir=user_dir)
            except Exception:
                pass
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
            match_attributes=thumb_data.match_attributes,
            match_score=thumb_data.match_score,
            data_dir=user_dir,
        )
        # Record negative signal for preference learning
        if thumb_data.match_attributes:
            try:
                preference_service.record_negative(
                    thumb_data.match_attributes, data_dir=user_dir)
            except Exception:
                pass
        return {"status": "ok", "entry": entry}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to save dislike"})


@app.post("/api/radio/skip")
async def radio_skip(request: Request):
    """Record an early skip for preference learning."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = SkipRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})
    if data.match_attributes:
        try:
            preference_service.record_skip(data.match_attributes, data_dir=user_dir)
        except Exception:
            pass
    return {"status": "ok"}


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
    return templates.TemplateResponse(request,"likes.html",
                                      _template_context(request, songs=liked, total=len(liked)))


@app.get("/radio/history", response_class=HTMLResponse)
async def radio_history_page(request: Request):
    """View radio play history."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    history = thumbs.load_history(data_dir=user_dir)
    history.reverse()  # newest first
    return templates.TemplateResponse(request,"history.html",
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
    channels = channel_service.load_channels(
        data_dir=user_dir, discogs_configured=_user_has_discogs(user))
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


@app.put("/api/radio/channels/{channel_id}/deep-cuts")
async def update_channel_deep_cuts_endpoint(channel_id: str, request: Request):
    """Toggle deep cuts mode for a channel."""
    user = request.state.user
    user_dir = _get_user_data_dir(user)
    try:
        body = await request.json()
        data = ChannelDeepCutsRequest(**body)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "Invalid request body"})

    try:
        channel = channel_service.update_channel_deep_cuts(
            channel_id, data.prefer_deep_cuts, data_dir=user_dir)
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
