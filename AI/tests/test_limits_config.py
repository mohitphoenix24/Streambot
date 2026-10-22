import pytest

from app.config import ConfigError, Settings
from app.core.limits import SlidingWindowLimiter


# ── rate limiter ─────────────────────────────────────────────────────────
def test_sliding_window_limiter():
    now = [0.0]
    limiter = SlidingWindowLimiter(limit=2, window_s=60, clock=lambda: now[0])
    assert limiter.check("a") is None
    assert limiter.check("a") is None
    assert limiter.check("a") == pytest.approx(60.0)  # blocked; wait ~a full window
    assert limiter.check("b") is None  # other clients unaffected
    now[0] = 59.0
    assert limiter.check("a") == pytest.approx(1.0)
    now[0] = 60.5
    assert limiter.check("a") is None  # window slid


def test_limit_zero_disables_the_limiter():
    limiter = SlidingWindowLimiter(limit=0)
    assert all(limiter.check("a") is None for _ in range(1000))


# ── config validation ────────────────────────────────────────────────────
def test_missing_model_and_key_fails_with_actionable_message():
    with pytest.raises(ConfigError) as err:
        Settings(openai_api_key=None, openai_model=None).validate()
    text = str(err.value)
    assert "OPENAI_MODEL" in text and "OPENAI_API_KEY" in text


@pytest.mark.parametrize("model", ["your_model", "", "  ", None])
def test_placeholder_model_is_rejected(model):
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        Settings(openai_api_key="sk-real", openai_model=model).validate()


def test_valid_openai_settings_pass():
    Settings(openai_api_key="sk-real", openai_model="some-model").validate()


def test_title_model_falls_back_to_main_model():
    assert Settings(openai_model="big").title_model == "big"
    assert Settings(openai_model="big", openai_title_model="small").title_model == "small"


def test_from_env_reads_variables_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("HISTORY_BUDGET_TOKENS", "500")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    s = Settings.from_env()
    assert s.history_budget_tokens == 500 and s.openai_model == "m"
    monkeypatch.setenv("HISTORY_BUDGET_TOKENS", "lots")
    with pytest.raises(ConfigError, match="HISTORY_BUDGET_TOKENS"):
        Settings.from_env()


def test_usd_to_inr_setting(monkeypatch):
    monkeypatch.delenv("USD_TO_INR", raising=False)
    assert Settings.from_env().usd_to_inr is None  # default: live ("auto")
    monkeypatch.setenv("USD_TO_INR", "AUTO")
    assert Settings.from_env().usd_to_inr is None
    monkeypatch.setenv("USD_TO_INR", "91.5")
    assert Settings.from_env().usd_to_inr == 91.5
    monkeypatch.setenv("USD_TO_INR", "-1")
    with pytest.raises(ConfigError, match="USD_TO_INR"):
        Settings.from_env()
