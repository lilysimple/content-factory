#!/usr/bin/env python3
"""Технический spike: запуск Claude Code из Python-подпроцесса.

**Это не bridge и не production-код.** Это инструмент, который отвечает на
пять вопросов из этапа 6 миграции и оставляет доказательство, а не мнение:

  1. какой способ авторизации работает;
  2. нужен ли ANTHROPIC_API_KEY;
  3. годится ли существующая авторизация Claude Code;
  4. какие переменные окружения необходимы;
  5. как безопасно запускать процесс из Python.

Запуск:

    ./.venv/bin/python deploy/spike-bridge.py          # без живых вызовов
    ./.venv/bin/python deploy/spike-bridge.py --live   # с вызовами модели

Живые сценарии стоят денег. Промпт в них намеренно вырожденный.

Почему окружение чистится. Если запускать этот скрипт из сессии Claude Code,
подпроцесс унаследует CLAUDECODE=1, CLAUDE_CODE_ENTRYPOINT=claude-desktop и
CLAUDE_CODE_MESSAGING_* — и поведёт себя не так, как поведёт себя завод под
launchd, где ничего этого нет. Тогда spike измерит собственную среду вместо
целевой. `clean_env()` собирает окружение, похожее на launchd-агентское.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Что launchd-агент реально даёт процессу. Всё остальное надо передавать явно.
KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SHELL")

# Переменные, которые выдают запуск изнутри Claude Code. Если они доехали до
# подпроцесса, замер недостоверен.
CONTAMINANTS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID")


def clean_env(**extra: str) -> dict[str, str]:
    """Окружение, приближенное к launchd-агентскому, плюс явно переданное."""
    env = {k: os.environ[k] for k in KEEP if k in os.environ}
    env.update({k: v for k, v in extra.items() if v})
    return env


def api_key_from_env_file() -> str:
    """Ключ из .env проекта. Читаем сами: python-dotenv грузит в os.environ,
    а нам нужно решать, класть его в окружение подпроцесса или нет."""
    path = ROOT / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def run_claude(prompt: str, *, env: dict[str, str], tools: str = "Read,Glob",
               timeout: int = 300, out_format: str = "text",
               cwd: Path = ROOT) -> dict:
    """Один вызов Claude Code. Так же, как это будет делать bridge.

    Безопасность запуска, по пунктам:
      * argv списком, без shell=True — строка запроса человека не попадает
        в шелл и не может из него выйти;
      * cwd задан явно, а не унаследован;
      * env собран белым списком, а не унаследован целиком;
      * timeout обязателен: висящий процесс держал бы задачу вечно;
      * stdout и stderr захвачены, в терминал ничего не утекает.
    """
    argv = [shutil.which("claude") or "claude", "-p", prompt,
            "--allowedTools", tools, "--output-format", out_format]
    started = time.monotonic()
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=timeout)
        return {"rc": p.returncode, "out": p.stdout.strip(),
                "err": p.stderr.strip(), "secs": round(time.monotonic() - started, 1)}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": "", "err": f"timeout {timeout}s",
                "secs": timeout}
    except FileNotFoundError:
        return {"rc": -2, "out": "", "err": "бинарь claude не найден в PATH",
                "secs": 0}


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'}  {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="делать настоящие вызовы модели (стоят денег)")
    args = ap.parse_args()

    print("\nSPIKE: запуск Claude Code из Python-подпроцесса\n")

    # ── 1. окружение ──────────────────────────────────────────────────
    print("Окружение")
    binary = shutil.which("claude")
    check("бинарь claude в PATH", bool(binary), binary or "не найден")

    dirty = [v for v in CONTAMINANTS if v in os.environ]
    check("запущено вне сессии Claude Code", not dirty,
          "унаследовано: " + ", ".join(dirty) if dirty
          else "окружение чистое")
    if dirty:
        print("     ↑ clean_env() их отрежет, но помни: этот замер сделан "
              "изнутри Claude Code")

    env = clean_env()
    check("в чистом окружении нет ANTHROPIC_API_KEY",
          "ANTHROPIC_API_KEY" not in env)
    check("в чистом окружении нет ANTHROPIC_BASE_URL",
          "ANTHROPIC_BASE_URL" not in env,
          "по умолчанию пойдёт на api.anthropic.com")

    # ── 2. хранилища авторизации ──────────────────────────────────────
    print("\nАвторизация: что лежит на диске")
    creds = Path.home() / ".claude" / ".credentials.json"
    check("~/.claude/.credentials.json", creds.exists(),
          "есть" if creds.exists() else "нет")

    kc = subprocess.run(["security", "find-generic-password", "-s",
                         "Claude Code-credentials"],
                        capture_output=True, text=True)
    check("keychain «Claude Code-credentials»", kc.returncode == 0,
          "есть" if kc.returncode == 0 else f"нет (exit {kc.returncode})")

    key = api_key_from_env_file()
    check("ANTHROPIC_API_KEY в .env проекта", bool(key),
          "есть" if key else "нет")

    cli_login = creds.exists() or kc.returncode == 0
    print(f"\n  Вывод: собственная авторизация CLI "
          f"{'ЕСТЬ' if cli_login else 'ОТСУТСТВУЕТ'}. "
          + ("" if cli_login else
             "Подписочный вход делается один раз командой `claude` → /login."))

    if not args.live:
        print("\nЖивые сценарии пропущены. Повтори с --live.\n")
        return 0

    # ── 3. живые сценарии ─────────────────────────────────────────────
    print("\nЖивые вызовы")
    ask = "Ответь ровно одним словом: ок"

    r = run_claude(ask, env=clean_env(), timeout=120)
    worked_bare = r["rc"] == 0 and "ок" in r["out"].lower()
    check("чистое окружение, без ключа", worked_bare,
          f"{r['secs']}с · {(r['out'] or r['err'])[:60]}")

    if key:
        r = run_claude(ask, env=clean_env(ANTHROPIC_API_KEY=key), timeout=120)
        check("чистое окружение + ANTHROPIC_API_KEY",
              r["rc"] == 0 and "ок" in r["out"].lower(),
              f"{r['secs']}с · {(r['out'] or r['err'])[:60]}")

    # ── 4. машинный вывод ─────────────────────────────────────────────
    print("\nМашинный вывод (нужен bridge, чтобы читать результат кодом)")
    r = run_claude(ask, env=clean_env(ANTHROPIC_API_KEY=key) if key else clean_env(),
                   out_format="json", timeout=120)
    data = {}
    if r["rc"] == 0:
        try:
            data = json.loads(r["out"])
        except json.JSONDecodeError:
            pass
    check("--output-format json разбирается", bool(data),
          ", ".join(sorted(data)[:8]) if data else "не разобрался")
    if data:
        for f in ("session_id", "total_cost_usd", "num_turns", "is_error"):
            if f in data:
                print(f"     {f}: {data[f]}")

    # ── 5. видимость субагентов ───────────────────────────────────────
    print("\nСубагенты проекта")
    r = run_claude("Сколько файлов в .claude/agents/? Ответь одним числом.",
                   env=clean_env(ANTHROPIC_API_KEY=key) if key else clean_env(),
                   tools="Read,Glob", timeout=180)
    check("субагенты видны из подпроцесса", r["rc"] == 0 and "6" in r["out"],
          f"{r['secs']}с · {(r['out'] or r['err'])[:60]}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
