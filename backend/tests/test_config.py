import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_embedding_dimension_accepts_environment_string() -> None:
    settings = Settings.model_validate({"embedding_dim": "1024"})

    assert settings.embedding_dim == 1024


def test_skill_persistent_authority_requires_explicit_auto_promotion_opt_in() -> None:
    assert Settings().skill_auto_promotion_enabled is False


def test_memory_save_deployment_switch_defaults_on_and_accepts_environment_string() -> None:
    assert Settings().memory_save_enabled is True
    assert Settings.model_validate({"memory_save_enabled": "false"}).memory_save_enabled is False


def test_provider_response_retry_policy_is_bounded() -> None:
    settings = Settings()

    assert settings.llm_provider_max_retries == 2
    assert settings.llm_provider_retry_base_delay_s == 0.5
    assert settings.llm_provider_retry_max_delay_s == 8.0
    with pytest.raises(ValidationError, match="基础退避"):
        Settings(
            llm_provider_retry_base_delay_s=9,
            llm_provider_retry_max_delay_s=8,
        )
