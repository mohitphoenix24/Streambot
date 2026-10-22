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
DATA_DIR = BASE_DIR / "data"  # SQLite file and the price table

# Values copied verbatim from .env.example. If one of these is still in place,
# the user forgot to edit the file; say so instead of sending it to OpenAI.
_PLACEHOLDERS = {"", "your_key", "your_model", "your_api_key", "your_model_name"}

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
    openai_timeout_s: float = 60.0

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    # Web search tool. No key = the tool is simply not offered.
    tavily_api_key: str | None = None
    web_search_max_results: int = 5
    max_output_tokens: int = 1024

    # The context window is the MODEL's hard limit. This is OUR budget for the
    # history part only. See history.py for why they are different numbers.
    history_budget_tokens: int = 6000

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

    database_path: Path = DATA_DIR / "chat.db"
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

    def validate(self) -> None:
        """Fail fast with an actionable message. Called once at server startup."""
        problems: list[str] = []
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
            openai_timeout_s=number("OPENAI_TIMEOUT_S", 60.0, minimum=1),
            system_prompt=text("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT,
            tavily_api_key=text("TAVILY_API_KEY"),
            web_search_max_results=number("WEB_SEARCH_MAX_RESULTS", 5, integer=True, minimum=1),
            max_output_tokens=number("MAX_OUTPUT_TOKENS", 1024, integer=True, minimum=16),
            history_budget_tokens=number("HISTORY_BUDGET_TOKENS", 6000, integer=True, minimum=1),
            usd_to_inr=None if (text("USD_TO_INR", "auto") or "auto").lower() == "auto"
            else number("USD_TO_INR", 0.0),
            usd_to_inr_fallback=number("USD_TO_INR_FALLBACK", 96.0, minimum=1),
            max_message_chars=number("MAX_MESSAGE_CHARS", 8000, integer=True, minimum=1),
            rate_limit_per_minute=number("RATE_LIMIT_PER_MINUTE", 20, integer=True),
            llm_max_attempts=number("LLM_MAX_ATTEMPTS", 2, integer=True, minimum=1),
            retry_base_delay_s=number("RETRY_BASE_DELAY_S", 0.5),
            partial_flush_interval_s=number("PARTIAL_FLUSH_INTERVAL_S", 1.0),
            database_path=path("DATABASE_PATH", DATA_DIR / "chat.db"),
            prices_path=path("PRICES_PATH", DATA_DIR / "prices.toml"),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()) if origins else (),
            log_level=(text("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
