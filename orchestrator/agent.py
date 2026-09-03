"""Вызов модели от лица роли.

Системный промпт собирается из трёх слоёв:
  roles/frame.md   общий каркас, одинаковый для всех
  roles/{role}.md  специфика роли
  маска            только для Ассистента, только на его реплики

Бюджет на тенанта в сутки — предохранитель. Круги переделки ограничены
двумя, но цикл может закольцеваться иначе, и упереться лучше в счётчик,
чем в счёт.

**Модель зовётся через CLI, а не через API.** Раньше здесь жил
`AsyncAnthropic` и ключ из `.env`, а Director ходил в `claude -p` — два
пути к одной модели, два счёта и два места, где чинить одну поломку.
Путь остался один, запуск живёт в `orchestrator/cli.py`, денег заводу
больше не нужно: авторизация идёт входом в CLI.

Цена переезда замерена 01.09 и оказалась мелкой: около 730 служебных
токенов и секунда на запуск процесса. Всё остальное — флаги, и они не
украшение, см. шапку `cli.py`. Кеш системного промпта работает так же,
как работал по API: второй вызов роли читает 94% префикса.

Что при этом потеряно честно: точки кеша больше не расставляются вручную.
Блоки `build_system` остались — они по-прежнему выстраивают промпт от
стабильного к изменчивому, и от этого порядка зависит попадание в кеш, —
но `cache_control` на них ставит CLI, а не мы.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from functools import lru_cache
from typing import Any

from config import ROOT, cfg
from orchestrator import cli
from orchestrator.personas import PERSONAS, default_persona
from storage import db

log = logging.getLogger("agent")

ROLES_DIR = ROOT / "roles"
MAX_ATTEMPTS = 3


class BudgetExceeded(RuntimeError):
    """Тенант выбрал дневной лимит вызовов."""


def reason(e: Exception) -> str:
    """Человеческая причина отказа, а не имя класса.

    Через неё проходят все роли, поэтому «войди в CLI» человек видит
    одинаково, кто бы ни упал. Разбор ответов CLI живёт в `cli.reason` —
    здесь только то, что относится к самому заводу.
    """
    if isinstance(e, BudgetExceeded):
        return str(e)
    if isinstance(e, cli.CliError):
        return str(e)
    return str(e) or type(e).__name__


@lru_cache(maxsize=32)
def _read_role(name: str) -> str:
    path = ROLES_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def system_text(role: str, *, brand_name: str = "", persona_id: str = "",
                profile: str = "", stable: str = "", extra: str = "") -> str:
    """Собрать системный промпт роли — от стабильного к изменчивому.

    Порядок частей это не стиль, а деньги. Кеширование — совпадение
    префикса: любое изменение обесценивает всё, что стоит после него.
    Поэтому части идут так и только так:

      1. frame + роль   одинаково у ВСЕХ тенантов → читается всеми
      2. маска + бренд  стабильно внутри тенанта
      3. stable         стабильно внутри задачи
      4. extra          меняется от вызова к вызову → всегда последним

    Чтение закешированного стоит примерно десятую часть обычного, и на
    втором вызове роли префикс читается на 94% — замерено 01.09 уже на
    новом пути. Точки кеша расставляет CLI сам; наше дело — не двигать
    изменчивое ближе к началу.

    Часть `stable` заведена для Дизайнера, и по делу. ТЗ площадки и
    эталонный макет у него ехали в теле запроса, то есть в некешируемом
    хвосте, а это самые объёмные вызовы завода: пока правка макета
    означала полную пересборку, за один и тот же эталон платили заново
    каждый круг. Роль, которая сюда ничего не передаёт, своего префикса
    не замечает — пустая часть не добавляется вовсе.
    """
    parts: list[str] = []

    core = "\n\n---\n\n".join(
        p for p in (_read_role("frame"), _read_role(role)) if p.strip())
    if core:
        parts.append(core)

    tenant_parts = []
    if brand_name:
        tenant_parts.append(f"Бренд, с которым ты работаешь: {brand_name}.")
    if role == "assistant" and persona_id:
        persona = PERSONAS.get(persona_id, default_persona())
        tenant_parts.append("## Маска\n\n" + persona.system_block())
    # Секции профиля читаются на каждом посте и внутри тенанта не меняются —
    # им место в стабильной части, а не в хвосте.
    if profile:
        tenant_parts.append("## Профиль бренда\n\n" + profile)
    if tenant_parts:
        parts.append("\n\n---\n\n".join(tenant_parts))

    if stable:
        parts.append(stable)

    # Изменчивое идёт последним. Всегда.
    if extra:
        parts.append(extra)

    return "\n\n---\n\n".join(parts)


JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def parse_json(raw: str, *, who: str = "модель") -> dict[str, Any]:
    """Разобрать JSON-ответ роли. Просили чистый, но подстраховаться дешевле.

    Роли, у которых выход это структура, а не текст, отвечают JSON. Модель
    иногда оборачивает его в ``` или приписывает строку до. Пустой словарь
    означает «разобрать не удалось» — вызывающий обязан это заметить.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if m := JSON_BLOCK.search(text):
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    log.error("%s вернула не JSON: %s", who, raw[:200])
    return {}


async def ask(
    role: str,
    chat_id: int,
    prompt: str,
    *,
    brand_name: str = "",
    persona_id: str = "",
    profile: str = "",
    stable: str = "",
    extra_system: str = "",
    max_tokens: int = 8000,
    effort: str = "",
    schema: dict[str, Any] | None = None,
) -> str:
    """Спросить модель от лица роли. Возвращает текст.

    `schema` — JSON Schema ответа. Передана — форма **гарантирована**, и
    `parse_json` разбирает ответ первой же строкой, без вылавливания
    регуляркой. Без неё роль просят про JSON только словами промпта, и
    любой сорвавшийся ответ стоит круга: у Редактора и Reels круг всего
    два, и один из них уходил на то, чтобы модель написала то же самое,
    но без пояснения вокруг.

    Схема должна быть закрытой: все поля в `required`, никаких
    дополнительных ключей. Слоты Дизайнера так не описывались, пока их
    набор был открытым; с 03.09 код знает его до вызова и передаёт
    схему (`design._schema`).

    `max_tokens` оставлен в сигнатуре, но потолком больше не управляет:
    у CLI его нет, длину ответа держит роль промптом. Роли передают его
    по-прежнему, и выкидывать параметр значило бы править девять мест
    ради строки — а вернуть его, если появится флаг, будет уже нечем.
    """
    used = db.bump_llm(chat_id, date.today().isoformat())
    if used > cfg.llm_budget_day:
        raise BudgetExceeded(
            f"дневной лимит {cfg.llm_budget_day} вызовов исчерпан")

    system = system_text(role, brand_name=brand_name, persona_id=persona_id,
                         profile=profile, stable=stable, extra=extra_system)

    delay = 2.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await cli.ask(
                role=role,
                system=system,
                prompt=prompt,
                model=cfg.model_for(role),
                effort=effort or cfg.effort_for(role),
                schema=schema,
            )
        except cli.CliError as e:
            # Повторять имеет смысл только то, что само пройдёт: лимит
            # запросов, сорвавшийся процесс. Отсутствие входа в CLI третья
            # попытка не починит — она только съест время и запутает лог.
            if not cli.retryable(str(e)):
                log.error("роль %s: повторять нечего — %s", role, e)
                raise
            log.warning("роль %s: попытка %s из %s — %s",
                        role, attempt, MAX_ATTEMPTS, e)
            if attempt == MAX_ATTEMPTS:
                raise
        await asyncio.sleep(delay)
        delay *= 2

    raise RuntimeError(f"роль {role}: модель не ответила после "
                       f"{MAX_ATTEMPTS} попыток")
