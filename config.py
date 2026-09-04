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
    # Ключа API здесь нет и быть не должно. Модель зовётся через CLI по
    # подписке, авторизация — вход `claude` в терминале, учётные данные
    # лежат в Keychain. Ключ в окружении увёл бы вызовы на API-тариф, то
    # есть на деньги; `cli.clean_env` его в подпроцесс и не пускает.
    model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

    # Модель и усилие на роль: MODEL_EDITOR, EFFORT_EDITOR. Распаковка и
    # стратегия это суждение, форматирование — нет, платить одинаково незачем.
    #
    # Настройки **старого пути**, того, что ходит через `agent.ask`. На путь
    # через Claude Code (`orchestrator/bridge.py`) не влияют и не должны:
    # `clean_env` не пропускает в подпроцесс ни `ANTHROPIC_MODEL`, ни
    # `MODEL_*`, там моделью распоряжается сам Claude Code.
    #
    # Роли здесь только те, что реально зовут модель. `publisher` не зовёт
    # её вовсе, `ideator` живёт субагентом Claude Code — ключи сняты 01.09,
    # бот и токен сняты 03.09: он поллился, не имея поведения.
    _ASK_ROLES = ("assistant", "research", "strategy", "editor", "reels", "design")

    role_models: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"MODEL_{role.upper()}", "") for role in Config._ASK_ROLES
    })
    role_effort: dict[str, str] = field(default_factory=lambda: {
        role: os.getenv(f"EFFORT_{role.upper()}", "") for role in Config._ASK_ROLES
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

    brands_path: Path = ROOT / os.getenv("BRANDS_PATH", "./Brands")
    db_path: Path = ROOT / os.getenv("DB_PATH", "./factory.db")
    default_tz: str = os.getenv("DEFAULT_TZ", "Europe/Moscow")
    llm_budget_day: int = int(os.getenv("LLM_BUDGET_PER_TENANT_DAY", "200"))

    # Ключ Pexels для `tools/stock_pull.py`. Завод его не зовёт: сток
    # приезжает в фотобанк руками человека, а не по ходу вёрстки —
    # Дизайнер не должен добирать картинку в момент сборки макета.
    pexels_key: str = os.getenv("PEXELS_API_KEY", "")

    # Ключ Gemini для генерации фона (`orchestrator/imagegen.py`).
    # Отдельный от модели завода намеренно: текст идёт через Claude Code
    # по подписке, ключа Anthropic у завода нет с 01.09, а картинки там
    # взять негде. Пусто — генерация не подмешивается к вариантам фона.
    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_image_model: str = os.getenv("GEMINI_IMAGE_MODEL",
                                        "gemini-3-pro-image-preview")

    def missing_tokens(self) -> list[str]:
        return [role for role, token in self.tokens.items() if not token]

    def chat_allowed(self, chat_id: int) -> bool:
        # Пустой allowlist — только для локальной отладки.
        return not self.allowed_chats or chat_id in self.allowed_chats


cfg = Config()
