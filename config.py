import logging
import re
import secrets

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    discogs_token: str = ""
    discogs_username: str = ""
    anthropic_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3:latest"
    # auto | compact | rich — "rich" gives local models the full curator prompt.
    # Worth setting for 24B+ local models; leave on auto for 8B-class models.
    prompt_tier: str = "auto"
    # off | flag | strict — how hard to fact-check AI recommendations against
    # public music catalogues before showing them. "flag" annotates and shows
    # everything; "strict" drops anything no catalogue can confirm.
    verification_policy: str = "flag"
    # Keep the generation audit log (which model produced what, and whether it
    # checked out). Disable only if you have a reason to.
    audit_enabled: bool = True
    audit_retention_days: int = 90

    # --- Local network access ---------------------------------------------
    # Off by default: the app binds to localhost and only this machine can
    # reach it. Turn on to let other people on the same network sign in with
    # a username and password. Does NOT expose anything to the internet —
    # that would additionally require a port forward, which you should not do
    # without HTTPS in front.
    lan_access: bool = False
    # Extra hostnames the app will answer to (comma separated). Private IPs
    # are accepted automatically when lan_access is on.
    extra_allowed_hosts: str = ""
    # Only enable behind a reverse proxy you control. X-Forwarded-For is
    # caller-supplied, so trusting it without a proxy lets anyone claim a
    # local address.
    trust_proxy_headers: bool = False
    app_name: str = "DiscogsRecommender/1.0"
    cache_ttl_seconds: int = 3600
    max_thumbs_entries: int = 500
    max_cache_entries: int = 1000
    secret_key: str = ""

    class Config:
        env_file = ".env"

    @field_validator("discogs_username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v:
            return v  # Allow empty — Discogs is optional
        if len(v) > 100:
            raise ValueError("discogs_username must be 1-100 characters")
        if not re.match(r"^[a-zA-Z0-9._-]+$", v):
            raise ValueError("discogs_username contains invalid characters")
        return v

    @field_validator("discogs_token")
    @classmethod
    def validate_discogs_token(cls, v: str) -> str:
        if not v:
            return v  # Allow empty — Discogs is optional
        if len(v) < 10:
            raise ValueError("discogs_token appears invalid")
        return v

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if v and not v.startswith("sk-ant-"):
            raise ValueError("anthropic_api_key must start with 'sk-ant-'")
        return v

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if not v:
            v = secrets.token_hex(32)
            logger.warning(
                "SECRET_KEY not set in .env — generated random key. "
                "Sessions will not survive restarts."
            )
        return v

    @field_validator("verification_policy")
    @classmethod
    def validate_verification_policy(cls, v: str) -> str:
        allowed = {"off", "flag", "strict"}
        v = (v or "flag").strip().lower()
        if v not in allowed:
            raise ValueError(f"verification_policy must be one of {sorted(allowed)}")
        return v

    @field_validator("prompt_tier")
    @classmethod
    def validate_prompt_tier(cls, v: str) -> str:
        allowed = {"auto", "compact", "rich"}
        v = (v or "auto").strip().lower()
        if v not in allowed:
            raise ValueError(f"prompt_tier must be one of {sorted(allowed)}")
        return v

    @property
    def discogs_configured(self) -> bool:
        """True when collection features can work at all.

        A username alone is enough: a public Discogs collection is readable
        over the REST API without a token. Database search still needs one —
        see DiscogsService.public_mode.
        """
        return bool(self.discogs_username)

    @property
    def discogs_public_mode(self) -> bool:
        """Collection readable, but no token — so no catalogue search."""
        return bool(self.discogs_username and not self.discogs_token)

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.extra_allowed_hosts.split(",") if h.strip()]

    @property
    def single_user_mode(self) -> bool:
        """Whether to auto-authenticate every visitor as the local admin.

        This is about whether there are real accounts to log into, which is a
        different question from whether collection features work. Naming a
        public Discogs username enables the collection but introduces no
        credentials, so it must not start demanding a login that the operator
        has no way to satisfy.
        """
        return not bool(self.discogs_token and self.discogs_username)

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        raise


settings = _load_settings()
