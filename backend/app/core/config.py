from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "DocuMind AI"
    app_env: str = "development"
    debug: bool = False

    database_url: str = "postgresql://postgres:postgres@localhost:5432/documind_ai"

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    pinecone_api_key: str = ""
    pinecone_index_name: str = "documind-ai"

    tavily_api_key: str = ""

    storage_dir: Path = Field(default=BACKEND_DIR / "storage")
    max_upload_mb: int = 25
    max_files_per_session: int = 20
    max_session_mb: int = 200
    max_concurrent_chats: int = 4

    cors_origins: str = "http://localhost:5173"

    @property
    def storage_path(self) -> Path:
        """Absolute storage dir; relative values resolve against the repo root, not the CWD."""
        p = Path(self.storage_dir)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy needs the psycopg3 driver prefix."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
