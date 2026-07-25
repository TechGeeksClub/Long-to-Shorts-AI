from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent

HUGGINGFACE_CACHE_ENV_VARS = (
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("LTS_DATA_DIR", ROOT_DIR / "data"))
    frontend_dist: Path = ROOT_DIR / "frontend" / "dist"
    ffmpeg: str = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe: str = os.getenv("FFPROBE_PATH", "ffprobe")
    whisper_model: str = os.getenv("WHISPER_MODEL", "small")
    min_clip_seconds: float = 30.0
    max_clip_seconds: float = 60.0
    candidate_count: int = int(os.getenv("LTS_CANDIDATE_COUNT", "10"))
    upload_chunk_size: int = 1024 * 1024
    minimum_free_bytes: int = 2 * 1024 * 1024 * 1024
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_seo_model: str = os.getenv("OLLAMA_SEO_MODEL", "qwen2.5:7b")
    ollama_seo_enabled: bool = os.getenv("OLLAMA_SEO_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    semantic_clip_enabled: bool = os.getenv("LTS_SEMANTIC_CLIP_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    semantic_clip_model: str = os.getenv("LTS_SEMANTIC_CLIP_MODEL", "qwen2.5:7b")
    semantic_candidate_count: int = int(os.getenv("LTS_SEMANTIC_CANDIDATES", "60"))
    ollama_timeout_seconds: float = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def huggingface_cache_dir(self) -> Path:
        return Path(os.getenv("LTS_HF_CACHE_DIR", self.data_dir / "hf_cache"))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.configure_huggingface_cache()

    def configure_huggingface_cache(self) -> None:
        fallback = self.huggingface_cache_dir
        fallback_hub = fallback / "hub"
        fallback_transformers = fallback / "transformers"

        configured_paths = [
            Path(value).expanduser()
            for name in HUGGINGFACE_CACHE_ENV_VARS
            if (value := os.getenv(name))
        ]
        needs_fallback = not configured_paths or any(
            not _ensure_writable_directory(path) for path in configured_paths
        )
        if not needs_fallback:
            return

        fallback_hub.mkdir(parents=True, exist_ok=True)
        fallback_transformers.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(fallback)
        os.environ["HF_HUB_CACHE"] = str(fallback_hub)
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(fallback_hub)
        os.environ["TRANSFORMERS_CACHE"] = str(fallback_transformers)

    def validate_binaries(self) -> list[str]:
        missing: list[str] = []
        if shutil.which(self.ffmpeg) is None and not Path(self.ffmpeg).exists():
            missing.append("FFmpeg")
        if shutil.which(self.ffprobe) is None and not Path(self.ffprobe).exists():
            missing.append("FFprobe")
        return missing


def _ensure_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


settings = Settings()
