"""Configuration - env vars and defaults."""

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv():
    """Load .env from cwd, walking up to home dir. No-op if python-dotenv missing."""
    try:
        from dotenv import load_dotenv
        # search cwd first, then parent dirs up to ~
        env_path = Path(".env")
        if not env_path.exists():
            cur = Path.cwd()
            home = Path.home()
            while cur != home and cur != cur.parent:
                candidate = cur / ".env"
                if candidate.exists():
                    env_path = candidate
                    break
                cur = cur.parent
        load_dotenv(env_path, override=False)
    except ImportError:
        pass  # python-dotenv not installed, silently skip


@dataclass
class Config:
    model: str = "gpt-5.5"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    provider: str = "openai"
    memory_enabled: bool = True
    memory_llm: str | None = None
    team_enabled: bool = False
    team_worktrees: bool = True           # isolate code-editing teammates by default
    team_max: int = 3
    team_model: str | None = None
    team_api_key: str | None = None
    team_base_url: str | None = None
    integration_model: str | None = None  # better model for merge-conflict reconciling
    integration_api_key: str | None = None
    integration_base_url: str | None = None
    checkpoint_enabled: bool = True       # breakpoint recovery (design_ckeckpoint.md)
    checkpoint_dir: str = ".CHECKPOINT"
    checkpoint_keep: int = 10             # snapshots compaction always keeps
    checkpoint_max: int = 50              # above this, compaction runs automatically

    @classmethod
    def from_env(cls) -> "Config":
        # load .env if present (won't override existing env vars)
        _load_dotenv()
        # pick up common env vars automatically
        api_key = (
            os.getenv("ENCODER_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or ""
        )
        return cls(
            model=os.getenv("ENCODER_MODEL", "gpt-5.5"),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("ENCODER_BASE_URL"),
            max_tokens=int(os.getenv("ENCODER_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("ENCODER_TEMPERATURE", "0")),
            max_context_tokens=int(os.getenv("ENCODER_MAX_CONTEXT", "128000")),
            memory_enabled=os.getenv("ENCODER_MEMORY_ENABLED", "1") != "0",
            memory_llm=os.getenv("ENCODER_MEMORY_LLM") or None,
            provider=os.getenv("ENCODER_PROVIDER", "openai"),
            team_enabled=os.getenv("ENCODER_TEAM_ENABLED", "0") == "1",
            team_worktrees=os.getenv("ENCODER_TEAM_WORKTREES", "1") != "0",
            team_max=int(os.getenv("ENCODER_TEAM_MAX", "3")),
            team_model=os.getenv("ENCODER_TEAM_MODEL") or None,
            team_api_key=os.getenv("ENCODER_TEAM_API_KEY") or None,
            team_base_url=os.getenv("ENCODER_TEAM_BASE_URL") or None,
            integration_model=os.getenv("ENCODER_INTEGRATION_MODEL") or None,
            integration_api_key=os.getenv("ENCODER_INTEGRATION_API_KEY") or None,
            integration_base_url=os.getenv("ENCODER_INTEGRATION_BASE_URL") or None,
            checkpoint_enabled=os.getenv("ENCODER_CHECKPOINT_ENABLED", "1") != "0",
            checkpoint_dir=os.getenv("ENCODER_CHECKPOINT_DIR", ".CHECKPOINT"),
            checkpoint_keep=int(os.getenv("ENCODER_CHECKPOINT_KEEP", "10")),
            checkpoint_max=int(os.getenv("ENCODER_CHECKPOINT_MAX", "50")),
        )
