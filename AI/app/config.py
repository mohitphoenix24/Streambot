"""Application configuration, read once from environment variables.

Why a settings object instead of os.environ["X"] scattered everywhere?
  * One place to see every knob the app has.
  * One place that validates values and fails EARLY with a message that says
    exactly what to fix (a bad config should crash at startup, not at the
    moment a user sends their first message).
  * Tests can build a Settings(...) directly and never touch the environment.

The model name is a setting on purpose. Model ids change, get retired, and
differ per account, so we never hardcode one and we never guess a price for it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]  # the AI/ directory
REPO_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"  # the price table

# Values copied verbatim from .env.example. If one of these is still in place,
# the user forgot to edit the file; say so instead of sending it to OpenAI.
_PLACEHOLDERS = {"", "your_key", "your_model", "your_api_key", "your_model_name", "your_jwt_secret", "change_me"}

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant in a chat application. "
    "Answer clearly. Use Markdown for code and lists when it helps."
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid. Message is user-facing."""


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_title_model: str | None = None
    openai_summary_model: str | None = None
    openai_timeout_s: float = 60.0

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # Web search tool. No key = the tool is simply not offered.
    tavily_api_key: str | None = None
    web_search_max_results: int = 5
    max_output_tokens: int = 1024

    # The context window is the MODEL's hard limit. This is OUR budget for the
    # history part only. See history.py for why they are different numbers.
    history_budget_tokens: int = 6000
    # Rolling memory: summarize the oldest messages instead of dropping them.
    memory_summary_enabled: bool = True

    # Display-only: OpenAI bills in USD; the UI shows an INR equivalent too.
    # None = fetch the live rate (USD_TO_INR=auto); a number = fixed rate; 0 = hide.
    usd_to_inr: float | None = None
    # Used only if the live lookup has never succeeded (INR per USD; ~96 on 2026-10-06).
    usd_to_inr_fallback: float = 96.0

    max_message_chars: int = 8000
    rate_limit_per_minute: int = 20

    llm_max_attempts: int = 2
    retry_base_delay_s: float = 0.5
    partial_flush_interval_s: float = 1.0

    # Authentication (JWT). The secret signs every login token: anyone who knows it can forge
    # a token for any user, so it must be long, random and private.
    jwt_secret: str | None = None
    jwt_expires_days: float = 7.0
    password_min_length: int = 8
    signup_enabled: bool = True
    auth_rate_limit_per_minute: int = 10  # login/signup attempts per client IP

    # Attachments (per-chat documents) and retrieval.
    embedding_model: str = "text-embedding-3-small"  # 1536 dimensions: the vector(1536) column depends on it
    max_upload_mb: int = 10
    max_documents_per_chat: int = 10
    upload_rate_limit_per_minute: int = 20  # uploads per user
    rag_max_pages: int = 300
    rag_max_chars: int = 2_000_000
    rag_max_chunks: int = 2000
    rag_chunk_chars: int = 1800  # about 450 tokens
    rag_overlap_chars: int = 200
    rag_top_k: int = 6  # passages handed to the model per search

    database_url: str = "postgresql://streambot:streambot@localhost:5440/streambot"
    db_pool_max: int = 10
    prices_path: Path = DATA_DIR / "prices.toml"
    # Exact allowed browser origins. Empty (the default) means "dev mode": allow the
    # React dev server on ANY localhost port, because Vite silently moves to
    # 5174, 5175... if 5173 is busy. In production set CORS_ORIGINS explicitly.
    cors_origins: tuple[str, ...] = ()
    log_level: str = "INFO"

    @property
    def cors_origin_regex(self) -> str | None:
        return None if self.cors_origins else r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

    @property
    def title_model(self) -> str | None:
        """Model for title generation: the cheap one if configured, else the main one."""
        return self.openai_title_model or self.openai_model

    @property
    def summary_model(self) -> str | None:
        """Model for memory summaries: its own setting, else the (cheap) title model."""
        return self.openai_summary_model or self.title_model

    def validate(self) -> None:
        """Fail fast with an actionable message. Called once at server startup."""
        problems: list[str] = []
        secret = (self.jwt_secret or "").strip()
        if secret.lower() in _PLACEHOLDERS or len(secret) < 32:
            problems.append(
                "JWT_SECRET is missing, a placeholder, or shorter than 32 characters. Generate one with:\n"
                "      python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
                "    and put it in .env (never commit it)."
            )
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            problems.append(
                "DATABASE_URL must be a PostgreSQL URL like postgresql://user:password@localhost:5440/streambot "
                "(start the database with `docker compose up -d postgres`)."
            )
        if (self.openai_api_key or "").strip().lower() in _PLACEHOLDERS:
            problems.append(
                "OPENAI_API_KEY is missing or still the placeholder. "
                "Set a real key in .env (never commit it)."
            )
        if (self.openai_model or "").strip().lower() in _PLACEHOLDERS:
            problems.append(
                "OPENAI_MODEL is missing or still the placeholder. Set it in .env to a "
                "model id your OpenAI account can use. This app intentionally has no "
                "default model."
            )
        if problems:
            raise ConfigError(
                "Cannot start, fix the configuration:\n  - " + "\n  - ".join(problems)
            )

    @classmethod
    def from_env(cls) -> "Settings":
        # override=False: a real environment variable beats the .env file, which is
        # how production deployments inject secrets.
        load_dotenv(REPO_ROOT / ".env", override=False)
        load_dotenv(BASE_DIR / ".env", override=False)

        def text(name: str, default: str | None = None) -> str | None:
            value = os.environ.get(name)
            return value.strip() if value and value.strip() else default

        def number(name: str, default: float, *, integer: bool = False, minimum: float = 0):
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = int(raw) if integer else float(raw)
            except ValueError:
                kind = "an integer" if integer else "a number"
                raise ConfigError(f"{name}={raw!r} must be {kind}.") from None
            if value < minimum:
                raise ConfigError(f"{name}={raw!r} must be >= {minimum}.")
            return value

        def path(name: str, default: Path) -> Path:
            raw = text(name)
            if raw is None:
                return default
            p = Path(raw).expanduser()
            return p if p.is_absolute() else BASE_DIR / p

        origins = text("CORS_ORIGINS")
        return cls(
            openai_api_key=text("OPENAI_API_KEY"),
            openai_model=text("OPENAI_MODEL"),
            openai_title_model=text("OPENAI_TITLE_MODEL"),
            openai_summary_model=text("OPENAI_SUMMARY_MODEL"),
            openai_timeout_s=number("OPENAI_TIMEOUT_S", 60.0, minimum=1),
            system_prompt=text("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT,
            tavily_api_key=text("TAVILY_API_KEY"),
            web_search_max_results=number("WEB_SEARCH_MAX_RESULTS", 5, integer=True, minimum=1),
            max_output_tokens=number("MAX_OUTPUT_TOKENS", 1024, integer=True, minimum=16),
            history_budget_tokens=number("HISTORY_BUDGET_TOKENS", 6000, integer=True, minimum=1),
            memory_summary_enabled=(text("MEMORY_SUMMARY_ENABLED", "true") or "true").lower() not in ("0", "false", "no", "off"),
            usd_to_inr=None if (text("USD_TO_INR", "auto") or "auto").lower() == "auto"
            else number("USD_TO_INR", 0.0),
            usd_to_inr_fallback=number("USD_TO_INR_FALLBACK", 96.0, minimum=1),
            max_message_chars=number("MAX_MESSAGE_CHARS", 8000, integer=True, minimum=1),
            rate_limit_per_minute=number("RATE_LIMIT_PER_MINUTE", 20, integer=True),
            llm_max_attempts=number("LLM_MAX_ATTEMPTS", 2, integer=True, minimum=1),
            retry_base_delay_s=number("RETRY_BASE_DELAY_S", 0.5),
            partial_flush_interval_s=number("PARTIAL_FLUSH_INTERVAL_S", 1.0),
            jwt_secret=text("JWT_SECRET"),
            jwt_expires_days=number("JWT_EXPIRES_DAYS", 7.0, minimum=0.01),
            password_min_length=number("PASSWORD_MIN_LENGTH", 8, integer=True, minimum=8),
            signup_enabled=(text("SIGNUP_ENABLED", "true") or "true").lower() not in ("0", "false", "no", "off"),
            auth_rate_limit_per_minute=number("AUTH_RATE_LIMIT_PER_MINUTE", 10, integer=True),
            embedding_model=text("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small") or "text-embedding-3-small",
            max_upload_mb=number("MAX_UPLOAD_MB", 10, integer=True, minimum=1),
            max_documents_per_chat=number("MAX_DOCUMENTS_PER_CHAT", 10, integer=True, minimum=1),
            upload_rate_limit_per_minute=number("UPLOAD_RATE_LIMIT_PER_MINUTE", 20, integer=True),
            rag_max_pages=number("RAG_MAX_PAGES", 300, integer=True, minimum=1),
            rag_chunk_chars=number("RAG_CHUNK_CHARS", 1800, integer=True, minimum=400),
            rag_overlap_chars=number("RAG_OVERLAP_CHARS", 200, integer=True, minimum=0),
            rag_top_k=number("RAG_TOP_K", 6, integer=True, minimum=1),
            database_url=text("DATABASE_URL", "postgresql://streambot:streambot@localhost:5440/streambot") or "",
            db_pool_max=number("DB_POOL_MAX", 10, integer=True, minimum=1),
            prices_path=path("PRICES_PATH", DATA_DIR / "prices.toml"),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()) if origins else (),
            log_level=(text("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
