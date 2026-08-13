"""Конфигурация из окружения. Единственное место, где читается .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent


def _chats(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    anthropic_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    tokens: dict[str, str] = field(default_factory=lambda: {
        "assistant": os.getenv("BOT_ASSISTANT", ""),
        "research":  os.getenv("BOT_RESEARCH", ""),
        "strategy":  os.getenv("BOT_STRATEGY", ""),
        "editor":    os.getenv("BOT_EDITOR", ""),
        "reels":     os.getenv("BOT_REELS", ""),
        "design":    os.getenv("BOT_DESIGN", ""),
        "publisher": os.getenv("BOT_PUBLISHER", ""),
    })

    allowed_chats: set[int] = field(
        default_factory=lambda: _chats(os.getenv("ALLOWED_CHATS", "")))
    miniapp_secret: str = os.getenv("MINIAPP_SECRET", "change-me")
    miniapp_url: str = os.getenv("MINIAPP_URL", "")

    brands_path: Path = ROOT / os.getenv("BRANDS_PATH", "./Brands")
    db_path: Path = ROOT / os.getenv("DB_PATH", "./factory.db")
    default_tz: str = os.getenv("DEFAULT_TZ", "Europe/Moscow")
    llm_budget_day: int = int(os.getenv("LLM_BUDGET_PER_TENANT_DAY", "200"))

    def missing_tokens(self) -> list[str]:
        return [role for role, token in self.tokens.items() if not token]

    def chat_allowed(self, chat_id: int) -> bool:
        # Пустой allowlist — только для локальной отладки.
        return not self.allowed_chats or chat_id in self.allowed_chats


cfg = Config()
