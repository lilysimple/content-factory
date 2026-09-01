"""Цикл 18: мост в Claude Code — окружение, задача, исходы.

Три границы, ради которых цикл написан.

**Окружение подпроцесса.** `config.py` зовёт `load_dotenv()`, поэтому в
`os.environ` процесса бота лежат ключ API и токены восьми ботов.
Унаследованное окружение отдало бы их наружу. Проверка на утечку тут не
формальность: она ловит правку, которая однажды покажется удобной.

**Мост не решает, кого звать.** В `input.md` не должно быть ни имён
субагентов, ни порядка вызова: это работа Director. Как только они там
появятся, выбор workflow окажется размазан по двум слоям.

**Исходы называются, а не проглатываются.** Процесс упал, `final.md` не
появился, вышло время — у каждого случая свой текст и своя запись в базе.
Молчание неотличимо от поломки.

Живых вызовов здесь нет: `claude` подменяется скриптом, который ведёт себя
как нужно сценарию.
"""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import harness
from harness import CHAT, check, report

harness.setup()

from config import cfg                                            # noqa: E402
from orchestrator import bridge                                   # noqa: E402
from storage import db                                            # noqa: E402

db.init(cfg.db_path)

SANDBOX = harness.TMP
bridge.TASKS_DIR = SANDBOX / "tasks"          # боевую tasks/ не трогаем
BIN = SANDBOX / "bin"
BIN.mkdir(parents=True, exist_ok=True)
os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ['PATH']}"

TODAY = "2026-08-29"


def fake_claude(body: str) -> None:
    """Подменить `claude` скриптом. Тело пишется на shell."""
    p = BIN / "claude"
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def row(task_id: str):
    return db.one("SELECT * FROM bridge_runs WHERE task_id = ?", task_id)


def reset() -> None:
    with db.tx() as c:
        c.execute("DELETE FROM bridge_runs")


async def main() -> None:
    # ── окружение: что уезжает в подпроцесс ───────────────────────────
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-НЕ-ДОЛЖЕН-УТЕЧЬ"
    os.environ["BOT_ASSISTANT"] = "111:НЕ-ДОЛЖЕН-УТЕЧЬ"
    env = bridge.clean_env()

    check("PATH передан", "PATH" in env)
    check("HOME передан", "HOME" in env)
    check("ANTHROPIC_API_KEY НЕ передан", "ANTHROPIC_API_KEY" not in env,
          "ключ уехал бы в подпроцесс и увёл вызов на API-тариф")
    check("токен бота НЕ передан", "BOT_ASSISTANT" not in env,
          "токены восьми ботов не должны покидать процесс")
    check("ничего лишнего вообще", set(env) <= set(bridge.KEEP),
          str(sorted(set(env) - set(bridge.KEEP))))

    # ── набор инструментов ────────────────────────────────────────────
    check("Task в наборе инструментов", "Task" in bridge.TOOLS,
          "без него Director не позовёт ни одного субагента")
    check("Read и Write в наборе",
          "Read" in bridge.TOOLS and "Write" in bridge.TOOLS)

    # ── final.md уезжает в Telegram с parse_mode=HTML ─────────────────
    # registry._send пробрасывает TelegramBadRequest дальше, поэтому одна
    # угловая скобка в прозе роняет отправку целиком. Задача при этом
    # успешна: время и лимиты потрачены, а человек видит молчание.
    f = bridge.for_telegram

    check("обычный текст не трогается", f("План недели готов.") ==
          "План недели готов.")
    check("парные теги живут", f("<b>План</b> на <i>неделю</i>") ==
          "<b>План</b> на <i>неделю</i>")
    check("ссылка живёт",
          f('<a href="https://t.me/x">канал</a>') ==
          '<a href="https://t.me/x">канал</a>')

    check("голая скобка экранируется", f("охват < 1000") == "охват &lt; 1000",
          f("охват < 1000"))
    check("амперсанд экранируется", f("Иванов & Ко") == "Иванов &amp; Ко",
          f("Иванов & Ко"))
    check("выдуманный тег становится текстом",
          f("<таблица>") == "&lt;таблица&gt;", f("<таблица>"))

    check("незакрытый тег становится текстом",
          f("<b>План") == "&lt;b&gt;План", f("<b>План"))
    check("непарное закрытие становится текстом",
          f("План</i>") == "План&lt;/i&gt;", f("План</i>"))
    check("перехлёст тегов не роняет отправку",
          "<b>" not in f("<b><i>x</b></i>").replace("<i>", "").replace("</i>", ""),
          f("<b><i>x</b></i>"))
    check("в очищенном тексте нет незакрытых тегов",
          f("<b>a<b>b</b>").count("<b>") == 1, f("<b>a<b>b</b>"))

    # ── заведение задачи ──────────────────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "Собери контент-план на неделю про агентов",
                             workflow="plan", today=TODAY,
                             brand_slug="lily-space", brand_path="/tmp/brand")
    d = bridge.TASKS_DIR / tid
    check("id по формату ГГГГ-ММ-ДД-workflow-NN", tid == f"{TODAY}-plan-01", tid)
    check("папка задачи создана", d.is_dir(), str(d))
    check("input.md написан", (d / "input.md").exists())

    text = (d / "input.md").read_text(encoding="utf-8")
    check("запрос человека внутри", "про агентов" in text)
    check("бренд назван", "lily-space" in text)
    check("final.md назван контрактом", "final.md" in text)

    low = text.lower()
    check("мост НЕ называет субагентов",
          not any(w in low for w in ("researcher", "strategist", "ideator",
                                     "writer", "designer")),
          "порядок ролей выбирает Director, а не Python")
    check("мост НЕ задаёт порядок вызовов",
          "→" not in text and "сначала" not in low)

    # ── факты в контракте: путь и свободные слоты ─────────────────────
    # Обе проверки про plan-05. Путь туда приезжал рабочим, но неопрятным
    # (`…/content-factory/../content-factory-brands/…`), и Director им не
    # воспользовался — пошёл угадывать относительный и промахнулся. Слоты
    # он считал сам, двумя запросами в базу мимо `orchestrator/strategy.py`:
    # ответ сошёлся, но у правила «семь дней с завтра» стало бы два дома.
    check("путь бренда нормализован", "/../" not in text,
          "неопрятному пути Director не верит и идёт угадывать")

    check("свободные слоты приехали фактом",
          "Окно плана и свободные слоты" in text,
          "иначе Director посчитает их сам, мимо strategy.free_slots")
    check("слоты названы парой «дата плюс площадка»",
          "telegram" in low and "-" in text)
    check("сказано не пересчитывать", "ересчитывать не надо" in text)
    check("назван источник правды", "free_slots" in text,
          "у окна и занятости один дом — orchestrator/strategy.py")

    # Отказ базы не должен валить задачу: без списка Director посчитает
    # сам, как и до правки. Пустой план хуже неточного контракта.
    import orchestrator.strategy as _strategy
    _real = _strategy.free_slots
    _strategy.free_slots = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("база недоступна"))
    try:
        block = "\n".join(bridge._slots(CHAT))
    finally:
        _strategy.free_slots = _real
    check("отказ подсчёта слотов не роняет задачу", "Слоты" in block)
    check("отказ назван, а не проглочен", "не удалось" in block, block[:80])

    r = row(tid)
    check("запись в базе заведена", r is not None)
    check("статус running", r and r["status"] == "running", r and r["status"])
    check("workflow записан", r and r["workflow"] == "plan")

    # ── одна задача за раз ────────────────────────────────────────────
    busy = False
    try:
        bridge.create_task(CHAT, "ещё одна", workflow="plan", today=TODAY)
    except bridge.Busy as e:
        busy, why = True, str(e)
    check("вторая задача отклонена", busy)
    check("в отказе назван id идущей", busy and why == tid, busy and why)

    # ── id не сталкиваются ────────────────────────────────────────────
    reset()
    second = bridge.create_task(CHAT, "вторая", workflow="plan", today=TODAY)
    check("следующий id не затирает первый", second == f"{TODAY}-plan-02",
          second)

    # ── номер занят в журнале, а папки нет ────────────────────────────
    # tasks/ лежит в .gitignore: папку не переживает ни уборка, ни свежий
    # клон, а task_id это PRIMARY KEY. Если new_id смотрит только на диск,
    # INSERT падает IntegrityError — и из on_plan это уходит человеку
    # молчанием вместо ответа.
    import shutil as _sh
    _sh.rmtree(bridge.TASKS_DIR / second)
    with db.tx() as c:                     # задача завершилась, чат свободен
        c.execute("UPDATE bridge_runs SET status = 'done' WHERE task_id = ?",
                  (second,))
    check("папка убрана, строка осталась",
          not (bridge.TASKS_DIR / second).exists() and row(second) is not None)
    third = bridge.create_task(CHAT, "после уборки", workflow="plan",
                               today=TODAY)
    check("занятый в журнале номер не переиспользуется", third != second,
          f"{third} == {second}")
    check("строка завелась, а не упала", row(third) is not None, third)

    # ── брошенный прогон не запирает чат навсегда ─────────────────────
    # launchd перезапустил завод посреди задачи, или инстанс сняли kill -9
    # (SIGTERM он не берёт). Строка осталась running, а процесса нет.
    # Без поправки «одна за раз» станет «ни одной больше никогда».
    reset()
    stale = bridge.create_task(CHAT, "прогон до перезапуска",
                               workflow="plan", today=TODAY)
    check("свежая строка держит мост", bridge.running() == stale,
          bridge.running())
    check("sweep не трогает свежий прогон", bridge.sweep() == 0)

    with db.tx() as c:
        c.execute("UPDATE bridge_runs SET started_at = "
                  "datetime('now', '-2 days') WHERE task_id = ?", (stale,))

    check("брошенная строка не считается идущей", bridge.running() == "",
          bridge.running())
    freed = bridge.create_task(CHAT, "после перезапуска",
                               workflow="plan", today=TODAY)
    check("новая задача заводится после брошенной", freed != stale, freed)
    check("свежая строка занимает мост обратно",
          bridge.running() == freed, bridge.running())

    # «Одна задача за раз» должно значить одну на мост, а не одну на чат:
    # два чата подняли бы два получасовых процесса на один tasks/ и одну
    # базу. Тенант сегодня один, поэтому поймать это можно только здесь.
    other = False
    try:
        bridge.create_task(CHAT + 1, "из другого чата",
                           workflow="plan", today=TODAY)
    except bridge.Busy:
        other = True
    check("чужой чат тоже упирается в занятость", other,
          "фильтр по chat_id превращал «одну за раз» в «одну на чат»")

    check("sweep пометил брошенный прогон", bridge.sweep() == 1)
    # ── отказ говорит по-человечески, а не блоком JSON ────────────────
    # Замерено живьём 30.08: на подписке самый вероятный отказ — лимит
    # сессии, и он единственный не имел своей ветки. Человек получал в
    # Telegram весь JSON от CLI вместо причины.
    limit_json = ('{"type":"result","is_error":true,"result":'
                  '"You\'ve hit your session limit \u00b7 resets 5:10am",'
                  '"session_id":"x","total_cost_usd":0}')
    said = "You've hit your session limit \u00b7 resets 5:10am"

    why = bridge._reason(limit_json, "", 1, said)
    check("лимит подписки назван словами", "лимит подписки" in why, why)
    check("в отказ не уехал JSON", "{" not in why and '"type"' not in why, why)
    check("время сброса сохранено", "5:10am" in why, why)
    check("лимит подписки не назван балансом", "средства" not in why, why)

    check("незнакомый отказ берёт реплику CLI, а не stdout",
          bridge._reason('{"result":"что-то пошло не так"}', "", 1,
                         "что-то пошло не так") == "что-то пошло не так")
    check("без реплики берётся stderr",
          bridge._reason("", "boom", 1) == "boom")
    check("вход в CLI по-прежнему узнаётся",
          "login" in bridge._reason("Not logged in", "", 1).lower())
    check("баланс по-прежнему узнаётся",
          "средства" in bridge._reason("credit balance too low", "", 1))

    r = row(stale)
    check("брошенный стал failed", r and r["status"] == "failed",
          r and r["status"])
    check("причина названа, а не пустая", r and r["error"], r and r["error"])
    r = row(freed)
    check("sweep не тронул идущий", r and r["status"] == "running",
          r and r["status"])

    # ── успешный прогон ───────────────────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    d = bridge.TASKS_DIR / tid
    fake_claude(
        f'printf "%s" "План недели готов." > "{d}/final.md"\n'
        f'printf "%s" "скелет" > "{d}/strategy.md"\n'
        'echo \'{"total_cost_usd": 0.42, "session_id": "abc", "is_error": false}\'')

    res = await bridge.run(tid)
    check("прогон успешен", res.ok, res.error)
    check("текст прочитан из final.md",
          res.text.startswith("План недели готов."), res.text[:60])
    # `strategy.md` здесь без машинного контракта, значит план не сел.
    # Молчание об этом хуже неполного ответа: человек прочитал бы «план
    # готов» и пошёл бы искать его в базе, где ничего нет.
    check("несостоявшаяся посадка названа человеку",
          "контракт" in res.text.lower(), res.text[:120])
    check("стоимость снята из JSON", res.cost == 0.42, str(res.cost))
    check("session_id снят", res.session_id == "abc", res.session_id)
    # Список читается человеком как «что отдали роли». Свои файлы Python в
    # него не приписывает: `input.md` это контракт, `stats.md` — снятые до
    # запуска цифры, и оба выглядели бы там чьей-то работой.
    check("артефакты перечислены",
          set(res.artifacts) == {"final.md", "strategy.md"},
          str(res.artifacts))
    check("контракт не выдан за результат роли",
          "input.md" not in res.artifacts)
    check("цифры не выданы за результат роли",
          bridge.STATS_FILE not in res.artifacts,
          "их снял Python до запуска, а не Ресёрчер")

    r = row(tid)
    check("статус done", r and r["status"] == "done", r and r["status"])
    check("длительность записана", r and r["duration_s"] is not None)
    check("стоимость записана как оценка",
          r and abs((r["estimated_api_cost"] or 0) - 0.42) < 1e-9,
          str(r and r["estimated_api_cost"]))
    check("finished_at проставлен", r and r["finished_at"])

    # ── грязный final.md доходит целиком ──────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    d = bridge.TASKS_DIR / tid
    fake_claude(
        f"""printf '%s' 'Охват < 1000 у <b>двух</b> тем. Сноска <незакрыта' """
        f'> "{d}/final.md"\n'
        'echo "{}"')
    res = await bridge.run(tid)
    check("грязный final.md не проваливает задачу", res.ok, res.error)
    check("парный тег дожил", "<b>двух</b>" in res.text, res.text)
    check("скобка в прозе обезврежена", "&lt; 1000" in res.text, res.text)
    check("незакрытое обезврежено", "&lt;незакрыта" in res.text, res.text)

    # ── отработал, но final.md нет ────────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    fake_claude('echo \'{"total_cost_usd": 0.1}\'')
    res = await bridge.run(tid)
    check("без final.md это провал", not res.ok)
    check("причина названа прямо", "final.md" in res.error, res.error)
    check("в базе failed", (r := row(tid)) and r["status"] == "failed",
          r and r["status"])
    check("стоимость записана и у провала", r and r["estimated_api_cost"],
          "провалившийся прогон тоже потрачен, его надо видеть")

    # ── провал объясняется словами Director ───────────────────────────
    # Первый живой прогон встал ровно тут: субагент ушёл в фон, ход
    # кончился, процесс умер вместе с фоном. Мост сказал только «файла
    # нет», и причину пришлось искать в транскрипте сессии.
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    fake_claude(
        'echo \'{"total_cost_usd": 0.93, "result": '
        '"Ресёрчер работает в фоне, подхвачу когда закончит."}\'')
    res = await bridge.run(tid)
    check("провал без final.md", not res.ok)
    check("последняя реплика Director снята", "фоне" in res.said, res.said)
    check("реплика попала в причину", "подхвачу" in res.error, res.error)
    check("причина всё ещё называет файл", "final.md" in res.error, res.error)

    # ── пустой final.md ───────────────────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    d = bridge.TASKS_DIR / tid
    fake_claude(f'printf "" > "{d}/final.md"\necho "{{}}"')
    res = await bridge.run(tid)
    check("пустой final.md это провал", not res.ok)
    check("сказано, что пустой", "пуст" in res.error, res.error)

    # ── процесс упал: не авторизован ──────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    fake_claude('echo "Not logged in · Please run /login" >&2\nexit 1')
    res = await bridge.run(tid)
    check("падение это провал", not res.ok)
    check("человеку сказано про /login", "/login" in res.error, res.error)
    check("причина человеческая, а не код возврата",
          "код" not in res.error.lower(), res.error)

    # ── процесс упал: лимит ───────────────────────────────────────────
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    fake_claude('echo "rate limit exceeded" >&2\nexit 1')
    res = await bridge.run(tid)
    check("лимит назван словами", "лимит" in res.error, res.error)

    # ── провал не прячет то, что успели сделать ───────────────────────
    # plan-04 упёрся в потолок на Стратеге, а research.md уже лежал: девять
    # минут живых запросов. Мост сказал только «не уложился», и работа
    # осталась невидимой — такую закажут заново.
    reset()
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    d = bridge.TASKS_DIR / tid
    fake_claude(f'printf "%s" "фактура" > "{d}/research.md"\necho "{{}}"')
    res = await bridge.run(tid)
    check("без final.md всё равно провал", not res.ok)
    check("сделанное названо в причине", "research.md" in res.error, res.error)
    check("сделанное перечислено в артефактах",
          "research.md" in res.artifacts, str(res.artifacts))
    check("input.md за достижение не выдаётся",
          "Успело лечь на диск: research.md" in res.error, res.error)

    # ── не уложился во время ──────────────────────────────────────────
    reset()
    was, bridge.TIMEOUT = bridge.TIMEOUT, 1
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    fake_claude("sleep 30")
    res = await bridge.run(tid)
    bridge.TIMEOUT = was
    check("потолок измерен, а не угадан", bridge.TIMEOUT >= 1800,
          f"три роли подряд в 900 с не уложились: {bridge.TIMEOUT}")
    check("зависший процесс не держит задачу вечно", not res.ok)
    check("сказано про время", "уложил" in res.error, res.error)
    check("в базе timeout", (r := row(tid)) and r["status"] == "timeout",
          r and r["status"])

    # ── бинарь не в PATH, но установлен ───────────────────────────────
    # Боевой отказ, найденный аудитом 30.08. launchd даёт агенту голый
    # PATH без `~/.local/bin`, и `shutil.which` возвращал None: из
    # терминала мост работал, из-под автозапуска падала любая задача.
    # Живой прогон делался руками, поэтому телеграм-нога цепи этого не
    # показала — ловим тестом, а не следующим боевым молчанием.
    reset()
    (BIN / "claude").rename(BIN / "claude-hidden")
    os.environ["PATH"] = str(BIN)                 # бинаря в PATH больше нет
    _fallback = bridge.FALLBACK_BINS
    bridge.FALLBACK_BINS = (str(BIN / "claude-hidden"),)
    check("бинарь находится мимо PATH", bridge.which_claude() ==
          str(BIN / "claude-hidden"), bridge.which_claude())

    # ── бинаря нет вообще ─────────────────────────────────────────────
    # Запасные пути тоже пусты, иначе тест нашёл бы настоящий CLI машины
    # и запустил его по-живому вместо заглушки.
    bridge.FALLBACK_BINS = (str(BIN / "нет-такого"),)
    check("пустой резолвер честно молчит", bridge.which_claude() == "",
          bridge.which_claude())
    tid = bridge.create_task(CHAT, "план", workflow="plan", today=TODAY)
    res = await bridge.run(tid)
    check("отсутствие claude названо", not res.ok and "claude" in res.error,
          res.error)

    bridge.FALLBACK_BINS = _fallback
    (BIN / "claude-hidden").rename(BIN / "claude")
    reset()

    # ── модель нового пути закреплена, и закреплена не Python-ом ──────
    # Умолчание CLI может смениться между прогонами и не оставить следа в
    # истории — на разных моделях замеры несравнимы. Пин живёт в
    # конфигурации Claude Code, потому что решение от 30.08 запрещает
    # Python вмешиваться в рантайм нового пути.
    import json as _json
    from config import ROOT as _ROOT
    conf = _ROOT / ".claude" / "settings.json"
    check("настройки Claude Code лежат в репозитории", conf.exists(), str(conf))
    if conf.exists():
        cfgj = _json.loads(conf.read_text(encoding="utf-8"))
        check("модель закреплена", cfgj.get("model") == "claude-opus-5",
              str(cfgj.get("model")))
        check("усилие закреплено", cfgj.get("effortLevel") == "medium",
              str(cfgj.get("effortLevel")))

    src = (_ROOT / "orchestrator" / "bridge.py").read_text(encoding="utf-8")
    check("мост НЕ передаёт --model", "--model" not in src,
          "модель задаётся конфигурацией Claude Code, не Python-ом")
    check("мост НЕ передаёт --effort", "--effort" not in src)
    check("настройки модели не уезжают в окружение",
          not any("MODEL" in k or "EFFORT" in k for k in bridge.KEEP),
          str(bridge.KEEP))

    # ── шесть точек входа ─────────────────────────────────────────────
    # В конце файла и на своей дате намеренно: задачи здесь занимают номера
    # id, и посреди сценария они сдвинули бы проверку «следующий id не
    # затирает первый». Стенд ловит такое сам — и поймал.
    OTHER_DAY = "2026-08-28"
    # Мост принимает не только план. Контекст при этом разный: плану нужны
    # свободные слоты, работе «по теме» — список тем, Ресёрчеру ни то ни
    # другое. Лишний блок это не мелочь: он едет в некешируемый хвост
    # каждого прогона.
    import re as _re
    rx = _re.compile(rf"^/({'|'.join(bridge.WORKFLOWS)})(@\S+)?(\s|$)")

    check("workflow больше одного", len(bridge.WORKFLOWS) >= 6,
          str(list(bridge.WORKFLOWS)))
    check("у каждого workflow есть контекст",
          set(bridge.CONTEXT) == set(bridge.WORKFLOWS),
          str(set(bridge.WORKFLOWS) ^ set(bridge.CONTEXT)))
    check("описания workflow по-русски и без имён ролей",
          not any(w in " ".join(bridge.WORKFLOWS.values()).lower()
                  for w in ("researcher", "strategist", "writer", "designer")),
          "кого звать — решает Director, а не Python")

    check("команда без аргументов ловится", bool(rx.match("/post")))
    check("команда с аргументом ловится",
          bool(rx.match("/post 2026-08-17-telegram-01")))
    check("суффикс @имя_бота ловится", bool(rx.match("/plan@lily_cf_bot")),
          "в супергруппе Telegram дописывает его сам")
    check("суффикс с аргументом ловится", bool(rx.match("/plan@lily_cf_bot про X")))
    check("похожая команда не перехватывается", not rx.match("/planning"))
    check("чужая команда не перехватывается", not rx.match("/publish"))
    check("обычный текст командой не перехватывается",
          not rx.match("напиши пост"))

    # ── просьба словами доходит до моста ──────────────────────────────
    # Критерий MVP: человек пишет «Собери контент-план на неделю», а не
    # команду. Пока входом была только команда, эта фраза уезжала старому
    # Стратегу по слову «план», и Director о ней не узнавал. Проверяем
    # обе половины: и что просьба доходит, и что старый путь не отрезан.
    from bots.handlers import AS_WORKFLOW, TOPIC
    from bots.router import resolve as _resolve

    _me = {"strategy": "lily_cf_strategy_bot", "editor": "lily_cf_editor_bot"}

    def _goes(text: str, topic: str | None = None):
        """Что случится с этой фразой: workflow моста или старый путь."""
        r = _resolve(text, topic, _me)
        wf = AS_WORKFLOW.get(r.role) if r.reason != "mention" else None
        return wf, r.role, r.reason

    wf, role, _ = _goes("Собери контент-план на неделю.")
    check("просьба словами уходит в мост", wf == "plan", f"{role} → {wf}")
    check("у workflow из просьбы есть топик для ответа", wf in TOPIC, str(wf))
    check("текст поста словами уходит в мост",
          _goes("напиши пост про агентов")[0] == "post")
    check("сценарий словами уходит в мост",
          _goes("сделай сценарий рилса")[0] == "reels")
    check("обложка словами уходит в мост",
          _goes("оформи обложку")[0] == "design")
    check("сводка словами уходит в мост",
          _goes("собери сводку, что зашло")[0] == "research")

    # Старый путь не переписан и остаётся доступен явным упоминанием —
    # иначе два пути нечем сравнивать, а этап 7 миграции ровно про это.
    check("упоминание бота оставляет задачу старому пути",
          _goes("@lily_cf_strategy_bot собери план")[0] is None,
          str(_goes("@lily_cf_strategy_bot собери план")))

    # Разговор о состоянии полчаса Claude Code не стоит.
    check("разговор в мост не уходит", _goes("покажи ядро")[0] is None,
          str(_goes("покажи ядро")))
    check("публикация в мост не уходит", _goes("опубликуй пост")[0] is None,
          str(_goes("опубликуй пост")))
    check("Публикатора нет среди workflow моста",
          "publisher" not in AS_WORKFLOW and "assistant" not in AS_WORKFLOW,
          str(sorted(AS_WORKFLOW)))
    check("каждый workflow из маршрутизатора знает мост",
          set(AS_WORKFLOW.values()) <= set(bridge.WORKFLOWS),
          str(set(AS_WORKFLOW.values()) - set(bridge.WORKFLOWS)))

    reset()
    tid_post = bridge.create_task(CHAT, "по теме 2026-08-17-telegram-01",
                                  workflow="post", today=OTHER_DAY)
    post_txt = (bridge.TASKS_DIR / tid_post / "input.md").read_text(encoding="utf-8")
    check("у поста в контракте темы, а не слоты",
          "Темы" in post_txt and "свободные слоты" not in post_txt.lower(),
          "лишний блок едет в некешируемый хвост каждого прогона")
    check("описание задачи взято из WORKFLOWS",
          bridge.WORKFLOWS["post"] in post_txt)

    # Рубрика едет фактом, а не догадкой роли.
    #
    # Старый путь её отдаёт (`design._brief`), мост не отдавал — и в
    # задаче `2026-09-01-design-01` Дизайнер написал, что списка рубрик
    # нет, и поставил её по смыслу поста. Совпало почти дословно, и это
    # везение: рубрика лежала в базе, её просто не показали.
    #
    # Тему сеем свою: к этому месту прогона база по темам пуста, и
    # проверка на чужом севе молча меряла бы пустую таблицу.
    reset()
    tid_rub = "2026-09-30-telegram-99"
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", (tid_rub,))
        c.execute("INSERT INTO themes (id, chat_id, date, plat, format, "
                  "rubric, status, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (tid_rub, CHAT, "2026-09-30", "telegram", "пост",
                   "Инструмент недели", "ready", "Тема с рубрикой"))
    tid_r = bridge.create_task(CHAT, f"свёрстай макет по теме {tid_rub}",
                               workflow="design", today=OTHER_DAY)
    rub_txt = (bridge.TASKS_DIR / tid_r / "input.md").read_text(encoding="utf-8")
    check("в шапке таблицы тем есть колонка рубрики",
          "| рубрика |" in rub_txt,
          "иначе роль назначает рубрику сама, имея её в базе")
    check("рубрика темы доехала в контракт",
          "Инструмент недели" in rub_txt,
          "Дизайнер придумает свою, имея эту в базе")
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id = ?", (tid_rub,))

    reset()
    tid_res = bridge.create_task(CHAT, "что зашло", workflow="research",
                                 today=OTHER_DAY)
    res_txt = (bridge.TASKS_DIR / tid_res / "input.md").read_text(encoding="utf-8")
    check("Ресёрчеру не кладут ни слоты, ни темы",
          "свободные слоты" not in res_txt.lower() and "| id |" not in res_txt,
          "он ходит наружу, а не в базу")


    reset()


asyncio.run(main())
raise SystemExit(report())
