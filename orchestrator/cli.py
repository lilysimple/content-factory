"""Запуск Claude Code. Единственное место, где завод трогает CLI.

Через этот модуль ходят оба потребителя:

  `orchestrator/agent.py`   роль спрашивает модель одним вызовом
  `orchestrator/bridge.py`  Director получает задачу и зовёт субагентов

Раньше здесь было два пути к модели: роли ходили в Anthropic API через
SDK, Director — в CLI. Два счёта, два способа авторизации и два места,
где чинить одну и ту же поломку. Теперь путь один, и деньги на API-балансе
заводу не нужны вовсе: авторизация идёт входом в CLI, ключа в проекте нет.

**Флаги вызова — не украшение, а цена.** Замерено 01.09 на тривиальном
запросе с настоящим системным промптом роли:

    без `--tools ""`            11 080 токенов служебного контекста
    с `--tools ""`                 731 токен
    cwd = папка завода          10 660 токенов (приезжает CLAUDE.md)
    cwd = нейтральная папка        729 токенов

Отсюда два правила, которые легко нарушить и невозможно заметить:

1. `--allowedTools ""` **не подходит**: он раздаёт права, а определения
   инструментов всё равно едут в контекст. Набор снимает `--tools ""`.
2. Роль запускается **вне репозитория**. Мост — наоборот, внутри: ему
   нужен `CLAUDE.md` и субагенты. Одна и та же папка стоит роли десять
   тысяч токенов на каждый вызов и не даёт ей ничего.

`--bare` в этот список не входит намеренно: он срезает контекст до нуля и
ломает авторизацию — `Not logged in`. Проверено, не возвращать.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("cli")

# Что получает подпроцесс. Всё остальное отрезается.
# ANTHROPIC_API_KEY сюда не входит намеренно — авторизация идёт входом CLI,
# учётные данные лежат в Keychain, и подпроцесс берёт их сам. Ключ в
# окружении увёл бы вызов на API-тариф, то есть на деньги вместо подписки.
KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL")

# Где искать `claude`, если его нет в PATH.
#
# Это не перестраховка, а починка боевого отказа. Завод стоит на launchd,
# а launchd даёт агенту голый PATH — `/usr/bin:/bin:/usr/sbin:/sbin`, без
# `~/.local/bin`, куда ставится CLI. Из терминала мост работал, из-под
# автозапуска `shutil.which("claude")` возвращал None, и **любая** задача
# из Telegram падала бы на первой строке с «бинарь не найден».
#
# Домашние пути разворачиваются от `HOME`, а не от текущего пользователя:
# на VPS завод пойдёт под своим аккаунтом.
FALLBACK_BINS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# Потолок на вызов роли. Не про скорость: зависший процесс не должен
# держать чат вечно. Мост считает свой потолок сам — там работа идёт
# минутами, а здесь один вызов.
ROLE_TIMEOUT = 300


class CliError(RuntimeError):
    """CLI не отработал. В тексте — человеческая причина, не код возврата."""


def clean_env() -> dict[str, str]:
    """Окружение подпроцесса. Белый список, а не наследование."""
    return {k: os.environ[k] for k in KEEP if k in os.environ}


def which_claude() -> str:
    """Путь к CLI. Сначала PATH, потом известные места установки.

    Возвращается абсолютный путь, поэтому подпроцессу хватает и голого
    PATH — искать себя ему уже не надо.
    """
    if found := shutil.which("claude"):
        return found
    for raw in FALLBACK_BINS:
        p = Path(raw).expanduser()
        if p.exists() and os.access(p, os.X_OK):
            log.info("claude найден мимо PATH: %s", p)
            return str(p)
    return ""


_neutral: Path | None = None


def neutral_dir() -> Path:
    """Папка, из которой запускается роль. Пустая и вне репозитория.

    `CLAUDE.md` ищется вверх по дереву, поэтому подпапка репозитория не
    годится: контекст Director приедет всё равно. Десять тысяч токенов на
    каждый вызов роли, которой он не нужен.
    """
    global _neutral
    if _neutral is None:
        _neutral = Path(tempfile.gettempdir()) / "content-factory-roles"
        _neutral.mkdir(parents=True, exist_ok=True)
    return _neutral


def reason(stdout: str, stderr: str, rc: int, said: str = "") -> str:
    """Человеческая причина отказа вместо кода возврата.

    Через неё проходит всё, что вернул CLI, поэтому «войди в CLI» человек
    видит одинаково, чем бы вызов ни упал.

    `said` — разобранное поле `result` из JSON CLI, то есть уже готовая
    человеческая строка. Раньше она игнорировалась, и в запасной ветке
    человеку уезжал **весь блок JSON**: при лимите сессии он получал в
    Telegram `{"type":"result","subtype":"error_during_execution",…}`
    вместо «упёрлись в лимит». Замерено живьём 30.08 — это самый вероятный
    отказ на подписке, и он единственный не имел своей ветки.

    Лимит подписки отделён от баланса намеренно: денег завод больше не
    тратит вовсе, и строка «закончились средства» отправила бы человека
    пополнять баланс, которому ничего не грозит.
    """
    blob = f"{stdout}\n{stderr}".lower()
    if "not logged in" in blob or "/login" in blob:
        return ("Claude Code не авторизован. Один раз выполни `claude` и "
                "`/login` в терминале")
    if "credit balance" in blob or "insufficient" in blob:
        return "закончились средства или исчерпан лимит"
    if "session limit" in blob or "usage limit" in blob:
        # Время сброса CLI кладёт в ту же строку — отдаём как есть.
        why = "упёрлись в лимит подписки Claude Code (не в баланс API)"
        return f"{why}. {said}" if said else why
    if "rate limit" in blob or "429" in blob:
        return "упёрлись в лимит запросов, попробуй позже"
    return (said or stderr or stdout or f"процесс вернул код {rc}")[:300]


def retryable(why: str) -> bool:
    """Стоит ли повторять. Лимит запросов — да, отсутствие входа — нет.

    Повтор на невходе только съедает время и путает лог: без `/login`
    третья попытка провалится ровно так же, как первая.
    """
    low = why.lower()
    if "не авторизован" in low or "средства" in low:
        return False
    return "лимит запросов" in low or "подписки" in low


def usage_line(role: str, data: dict[str, Any]) -> None:
    """Записать в лог, во что обошёлся вызов.

    Кеш работает молча, поэтому его надо видеть: если чтение стабильно
    нулевое при одинаковом промпте, значит в начало уехало что-то
    изменчивое, и мы платим полную цену за каждый вызов.
    """
    u = data.get("usage") or {}
    read = u.get("cache_read_input_tokens", 0) or 0
    write = u.get("cache_creation_input_tokens", 0) or 0
    fresh = u.get("input_tokens", 0) or 0
    total = read + write + fresh
    share = f"{read * 100 // total}%" if total else "—"
    log.info("%s: вход %s (из кеша %s, %s; записано %s), выход %s",
             role, total, read, share, write, u.get("output_tokens", 0))


async def ask(
    *,
    role: str,
    system: str,
    prompt: str,
    model: str = "",
    effort: str = "",
    schema: dict[str, Any] | None = None,
    timeout: int = ROLE_TIMEOUT,
) -> str:
    """Один вызов модели от лица роли. Возвращает текст ответа.

    Инструментов у роли нет вовсе: она пишет текст, а не ходит по файлам.
    Это и поведение (роль не уйдёт читать репозиторий), и цена — см.
    замеры в шапке модуля.
    """
    binary = which_claude()
    if not binary:
        raise CliError("бинарь claude не найден: ни в PATH, ни в "
                       + ", ".join(FALLBACK_BINS))

    argv = [binary, "-p", prompt,
            "--system-prompt", system,
            "--tools", "",                  # НЕ --allowedTools, см. шапку
            "--strict-mcp-config",
            "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if schema:
        argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(neutral_dir()), env=clean_env(),
            # Без DEVNULL CLI три секунды ждёт данных на stdin: запрос уже
            # лежит в argv, ждать нечего.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise CliError(f"не удалось запустить процесс: {e}") from e

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CliError(f"роль {role} не ответила за {timeout} с") from None

    stdout = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()

    data: dict[str, Any] = {}
    if stdout:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            log.warning("%s: stdout не разобрался как JSON", role)

    said = str(data.get("result") or "").strip()
    if proc.returncode != 0 or data.get("is_error"):
        raise CliError(reason(stdout, stderr, proc.returncode or 1, said))

    usage_line(role, data)
    if not said:
        raise CliError(f"роль {role} вернула пустой ответ")
    return said
