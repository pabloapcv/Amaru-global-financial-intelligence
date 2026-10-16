"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=value lines; tolerates `export KEY=...`, quotes, a BOM and trailing comments."""
    out: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'") and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for key, value in parse_dotenv(env_file.read_text()).items():
        # A variable exported empty in the shell should not mask a value in .env.
        if not os.environ.get(key):
            os.environ[key] = value


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("ATLAS_DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'atlas.db'}")
    )
    object_store: Path = field(
        default_factory=lambda: Path(os.getenv("ATLAS_OBJECT_STORE", str(ROOT / "data" / "objects")))
    )
    # LLM settings. The research engine falls back to a deterministic planner when no
    # Anthropic credentials are configured, so the platform still works offline.
    llm_model: str = field(default_factory=lambda: os.getenv("ATLAS_LLM_MODEL", "claude-opus-5-5"))
    llm_effort: str = field(default_factory=lambda: os.getenv("ATLAS_LLM_EFFORT", "high"))
    llm_enabled: bool = field(
        default_factory=lambda: os.getenv("ATLAS_LLM", "auto").lower() != "off"
    )
    max_research_tool_calls: int = field(
        default_factory=lambda: int(os.getenv("ATLAS_MAX_TOOL_CALLS", "24"))
    )
    http_timeout: float = 30.0
    # Identify ATLAS honestly to data providers, with a contact URL (crawler etiquette).
    user_agent: str = field(
        default_factory=lambda: os.getenv("ATLAS_USER_AGENT", "ATLAS-research/0.1 (+https://github.com/pabloapcv/atlas-fin)")
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            os.getenv("ATLAS_CORS_ORIGINS", "http://localhost:3010,http://127.0.0.1:3010").split(",")
        )
    )


settings = Settings()
