"""Вызов модели от лица роли.

Системный промпт собирается из трёх слоёв:
  roles/frame.md   общий каркас, одинаковый для всех
  roles/{role}.md  специфика роли
  маска            только для Ассистента, только на его реплики

Бюджет на тенанта в сутки — предохранитель. Круги переделки ограничены
двумя, но цикл может закольцеваться иначе, и упереться лучше в счётчик,
чем в счёт.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from anthropic import APIError, AsyncAnthropic, RateLimitError

from config import ROOT, cfg
from orchestrator.personas import PERSONAS, default_persona
from storage import db

log = logging.getLogger("agent")

ROLES_DIR = ROOT / "roles"
MAX_ATTEMPTS = 3


class BudgetExceeded(RuntimeError):
    """Тенант выбрал дневной лимит вызовов."""


def _reason(e: APIError) -> str:
    """Человеческая причина отказа, а не дамп JSON в чат."""
    body = getattr(e, "body", None) or {}
    msg = (body.get("error") or {}).get("message", "") if isinstance(body, dict) else ""
    low = msg.lower()
    if "credit balance" in low:
        return ("на аккаунте API закончились средства. "
                "console.anthropic.com → Plans & Billing")
    if "authentication" in low or "invalid x-api-key" in low:
        return "ключ API не принят, проверь ANTHROPIC_API_KEY"
    if "max_tokens" in low or "too long" in low:
        return "запрос длиннее лимита модели"
    return msg or str(e)


@lru_cache(maxsize=32)
def _read_role(name: str) -> str:
    path = ROLES_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_system(role: str, *, brand_name: str = "", persona_id: str = "",
                 extra: str = "") -> str:
    """Собрать системный промпт роли."""
    parts = [_read_role("frame"), _read_role(role)]

    if role == "assistant" and persona_id:
        persona = PERSONAS.get(persona_id, default_persona())
        parts.append("## Маска\n\n" + persona.system_block())

    if brand_name:
        parts.append(f"Бренд, с которым ты работаешь: {brand_name}.")
    if extra:
        parts.append(extra)

    return "\n\n---\n\n".join(p for p in parts if p.strip())


_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not cfg.anthropic_key:
            raise RuntimeError("нет ANTHROPIC_API_KEY")
        _client = AsyncAnthropic(api_key=cfg.anthropic_key)
    return _client


async def ask(
    role: str,
    chat_id: int,
    prompt: str,
    *,
    brand_name: str = "",
    persona_id: str = "",
    extra_system: str = "",
    max_tokens: int = 8000,
    effort: str = "",
) -> str:
    """Спросить модель от лица роли. Возвращает текст.

    Про max_tokens: на Opus 5 мышление включено по умолчанию, и лимит
    считает мышление вместе с ответом. Скупой max_tokens обрезает ответ
    на середине, поэтому запас здесь не роскошь.

    Параметра temperature нет намеренно: на Opus 5 он снят и возвращает 400.
    Тон задаётся промптом, а не сэмплингом.
    """
    used = db.bump_llm(chat_id, date.today().isoformat())
    if used > cfg.llm_budget_day:
        raise BudgetExceeded(
            f"дневной лимит {cfg.llm_budget_day} вызовов исчерпан")

    system = build_system(role, brand_name=brand_name,
                          persona_id=persona_id, extra=extra_system)
    model = cfg.model_for(role)

    delay = 2.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                output_config={"effort": effort or cfg.effort_for(role)},
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text").strip()

        except RateLimitError:
            log.warning("rate limit, попытка %s из %s", attempt, MAX_ATTEMPTS)
        except APIError as e:
            # 4xx кроме 429 это отказ по сути запроса: нет средств, кривой
            # ключ, слишком длинный контекст. Повтор ничего не изменит,
            # только съест время и запутает лог.
            status = getattr(e, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                log.error("запрос отклонён (%s), повторять нечего: %s",
                          status, _reason(e))
                raise
            log.error("ошибка API (%s из %s): %s", attempt, MAX_ATTEMPTS, e)
            if attempt == MAX_ATTEMPTS:
                raise
        await asyncio.sleep(delay)
        delay *= 2

    raise RuntimeError(f"роль {role}: модель не ответила после "
                       f"{MAX_ATTEMPTS} попыток")
