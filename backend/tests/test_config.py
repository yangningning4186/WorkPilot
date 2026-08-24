from app.core.config import Settings


def test_embedding_dimension_accepts_environment_string() -> None:
    settings = Settings.model_validate({"embedding_dim": "1024"})

    assert settings.embedding_dim == 1024
