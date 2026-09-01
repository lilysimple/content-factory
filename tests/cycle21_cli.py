"""Цикл 21: вызов роли через Claude Code — флаги, окружение, исходы.

Роли ходят в модель тем же CLI, что и Director. Путь через Anthropic API
снят: денег заводу не нужно, авторизация — вход `claude` в терминале.

Три границы, ради которых цикл написан.

**Флаги — это цена.** Замерено 01.09 на настоящем системном промпте:
`--allowedTools ""` оставляет в контексте 11 080 токенов определений
инструментов, `--tools ""` — 731. Разница невидима: ответ приходит
одинаковый, платится втрое дороже. Проверка держит именно её, потому что
заметить такую правку глазами нельзя.

**Роль запускается вне репозитория.** `CLAUDE.md` ищется вверх по дереву,
и в папке завода к каждому вызову роли приезжает контекст Director —
10 660 токенов вместо 729. Роли он не нужен: она пишет текст.

**Ключ API не уезжает в подпроцесс.** Даже если он лежит в окружении,
завод ходит по подписке. Ключ увёл бы вызовы на API-тариф молча.

Живых вызовов здесь нет: `claude` подменяется скриптом, который
записывает, с чем его позвали, и отвечает по сценарию.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import agent, cli                                # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

SANDBOX = harness.TMP
BIN = SANDBOX / "bin"
BIN.mkdir(parents=True, exist_ok=True)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"

CALL = SANDBOX / "call.json"          # чем позвали: argv, cwd, окружение


def fake_claude(body: str) -> None:
    """Подменить `claude` скриптом, который сначала пишет протокол вызова.

    Протокол пишет сам подменённый бинарь, а не тест: только так видно
    argv и окружение **подпроцесса**, а не то, что мы собирались передать.
    """
    script = f'''#!/bin/sh
{{
  printf '{{"argv": ['
  sep=""
  for a in "$@"; do
    printf '%s"%s"' "$sep" "$(printf '%s' "$a" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g' | tr '\\n' ' ')"
    sep=", "
  done
  printf '], "cwd": "%s", "has_key": "%s"}}' "$(pwd)" "${{ANTHROPIC_API_KEY:-нет}}"
}} > "{CALL}"
{body}
'''
    p = BIN / "claude"
    p.write_text(script, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def called() -> dict:
    return json.loads(CALL.read_text(encoding="utf-8"))


def ok(result: str) -> str:
    """Тело подделки: ответить как удачный прогон CLI."""
    payload = json.dumps({"type": "result", "is_error": False,
                          "result": result,
                          "usage": {"input_tokens": 10,
                                    "cache_read_input_tokens": 900,
                                    "cache_creation_input_tokens": 0,
                                    "output_tokens": 20}},
                         ensure_ascii=False)
    return f"cat <<'JSON'\n{payload}\nJSON"


SCHEMA = {"type": "object",
          "properties": {"text": {"type": "string"}},
          "required": ["text"], "additionalProperties": False}


async def main() -> None:
    # ── флаги вызова ──────────────────────────────────────────────────
    fake_claude(ok('{"text": "готово"}'))
    out = await agent.ask("editor", CHAT, "напиши", brand_name="Lily Space",
                          schema=SCHEMA, effort="low")
    check("ответ берётся из поля result", out == '{"text": "готово"}', out)

    argv = called()["argv"]
    joined = " ".join(argv)

    # Главная проверка цикла. `--allowedTools` раздаёт права, набор
    # инструментов при этом едет в контекст целиком: 11 080 токенов
    # против 731. Подмена одного флага другим выглядит безобидно и
    # обходится втрое дороже на каждом вызове.
    check("набор инструментов снят через --tools", "--tools" in argv,
          joined[:200])
    check("--allowedTools не используется", "--allowedTools" not in argv,
          "это права, а не набор: контекст всё равно приедет")
    check("чужие MCP не приезжают", "--strict-mcp-config" in argv, joined[:200])
    check("схема доехала до CLI", "--json-schema" in argv, joined[:200])
    check("усилие роли доехало",
          "--effort" in argv and "low" in argv, joined[:200])
    check("модель названа явно", "--model" in argv, joined[:200])
    check("формат ответа машинный",
          "--output-format" in argv and "json" in argv, joined[:200])

    # Промпт роли уезжает системным, а не приклеивается к запросу: от
    # этого зависит попадание в кеш, а с ним цена каждого вызова.
    check("промпт роли уехал системным", "--system-prompt" in argv,
          joined[:200])
    sys_at = argv.index("--system-prompt") + 1
    check("в системном промпте каркас и роль",
          "frame" in argv[sys_at] or "Редактор" in argv[sys_at],
          argv[sys_at][:120])

    # ── где запускается ───────────────────────────────────────────────
    repo = str(Path(__file__).resolve().parents[1])
    check("роль работает вне репозитория", not called()["cwd"].startswith(repo),
          f"{called()['cwd']}: там CLAUDE.md, это 10 660 токенов на вызов")

    # ── окружение ─────────────────────────────────────────────────────
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-НЕ-ДОЛЖЕН-УЕХАТЬ"
    try:
        fake_claude(ok('{"text": "готово"}'))
        await agent.ask("editor", CHAT, "напиши")
        check("ключ API не уезжает в подпроцесс",
              called()["has_key"] == "нет",
              "ключ в окружении уводит вызов на API-тариф молча")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    check("в белом списке нет ключа", "ANTHROPIC_API_KEY" not in cli.KEEP,
          str(cli.KEEP))

    # ── исходы ────────────────────────────────────────────────────────
    fake_claude('echo "Not logged in · Please run /login" >&2\nexit 1')
    try:
        await agent.ask("editor", CHAT, "напиши")
        check("невход это отказ", False, "вызов прошёл, а не должен был")
    except cli.CliError as e:
        check("невход это отказ", True)
        check("человеку сказано про /login", "/login" in str(e), str(e))

    # Повторять невход бессмысленно: третья попытка провалится так же.
    check("невход не повторяется", not cli.retryable("Claude Code не авторизован"))
    check("лимит запросов повторяется", cli.retryable("упёрлись в лимит запросов"))

    fake_claude(ok(""))
    try:
        await agent.ask("editor", CHAT, "напиши")
        check("пустой ответ это отказ", False, "пустое сошло за работу")
    except cli.CliError as e:
        check("пустой ответ это отказ", "пуст" in str(e), str(e))

    # ── бюджет тенанта ────────────────────────────────────────────────
    # Предохранитель остался тем же: круги переделки ограничены двумя, но
    # цикл может закольцеваться иначе, и упереться лучше в счётчик.
    # Счётчик подводим к потолку через базу, а не подменой конфига:
    # `Config` заморожен намеренно, и обходить это в тесте значило бы
    # проверять не тот код, который работает в бою.
    fake_claude(ok('{"text": "готово"}'))
    with db.tx() as c:
        c.execute("INSERT INTO llm_usage (chat_id, day, calls) "
                  "VALUES (?, date('now'), ?) ON CONFLICT(chat_id, day) "
                  "DO UPDATE SET calls = excluded.calls",
                  (CHAT, cfg.llm_budget_day))
    try:
        await agent.ask("editor", CHAT, "напиши")
        check("дневной лимит держит", False, "лимит не сработал")
    except agent.BudgetExceeded as e:
        check("дневной лимит держит", "лимит" in str(e), str(e))


asyncio.run(main())
raise SystemExit(report())
