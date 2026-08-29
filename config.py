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
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

    # Модель и усилие можно задать отдельно на роль: MODEL_EDITOR, EFFORT_EDITOR.
    # Распаковка и стратегия это суждение, публикация это форматирование —
    # платить за них одинаково смысла нет.
    role_models: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"MODEL_{role.upper()}", "")
        for role in ("assistant", "research", "strategy", "ideator", "editor",
                     "reels", "design", "publisher")
    })
    role_effort: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"EFFORT_{role.upper()}", "")
        for role in ("assistant", "research", "strategy", "ideator", "editor",
                     "reels", "design", "publisher")
    })
    default_effort: str = os.getenv("ANTHROPIC_EFFORT", "high")

    def model_for(self, role: str) -> str:
        return self.role_models.get(role) or self.model

    def effort_for(self, role: str) -> str:
        return self.role_effort.get(role) or self.default_effort

    tokens: dict[str, str] = field(default_factory=lambda: {
        "assistant": os.getenv("BOT_ASSISTANT", ""),
        "research":  os.getenv("BOT_RESEARCH", ""),
        "strategy":  os.getenv("BOT_STRATEGY", ""),
        "ideator":   os.getenv("BOT_IDEATOR", ""),
        "editor":    os.getenv("BOT_EDITOR", ""),
        "reels":     os.getenv("BOT_REELS", ""),
        "design":    os.getenv("BOT_DESIGN", ""),
        "publisher": os.getenv("BOT_PUBLISHER", ""),
    })

    allowed_chats: set[int] = field(
        default_factory=lambda: _chats(os.getenv("ALLOWED_CHATS", "")))
    # Куда публикует Публикатор. Числовой id канала (-100…) или @имя.
    # Пусто — публикация выключена, и он об этом говорит вслух.
    publish_channel: str = os.getenv("PUBLISH_CHANNEL", "")

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
