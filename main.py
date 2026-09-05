"""Точка входа: семь ботов, два диспетчера.

Разные allowed_updates — не оптимизация, а необходимость. callback_query
доставляется тому боту, который отправил сообщение с кнопкой, поэтому
нажатия слушают все семеро. Сообщения людей — только Ассистент.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher

from bots import handlers
from bots.registry import registry
from config import cfg
from orchestrator import bridge
from storage import db

ASSISTANT_UPDATES = ["message", "edited_message", "my_chat_member",
                     "chat_member", "callback_query"]
WORKER_UPDATES = ["callback_query"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("main")


async def amain() -> None:
    missing = cfg.missing_tokens()
    if "assistant" in missing:
        raise SystemExit("Нет BOT_ASSISTANT — без него ничего не работает. "
                         "Скопируй .env.example в .env и заполни.")
    if missing:
        log.warning("нет токенов для ролей: %s", ", ".join(missing))
    if not cfg.allowed_chats:
        log.warning("ALLOWED_CHATS пуст — принимаю любой чат. "
                    "Так можно только локально.")

    cfg.brands_path.mkdir(parents=True, exist_ok=True)
    db.init(cfg.db_path)

    # Починить журнал моста до того, как примем первую задачу. Брошенные
    # прогоны появляются ровно здесь: процесс с задачей в работе не
    # переживает перезапуск, а строка остаётся running. `bridge.running`
    # такую строку и так не считает идущей, но в журнале она врала бы
    # вечно идущей задачей.
    if n := bridge.sweep():
        log.warning("брошенных прогонов моста закрыто: %s", n)

    # Строка очереди, взятая процессом, которого больше нет, не идёт и не
    # ждёт — то есть просто пропала, а человек ждёт ответа. Возвращаем.
    if n := bridge.unstick():
        log.warning("строк очереди возвращено в работу: %s", n)

    await registry.start()

    dp_assistant = Dispatcher()
    dp_workers = Dispatcher()
    handlers.register(dp_assistant, dp_workers)

    workers = [b for role, b in registry.bots.items() if role != "assistant"]
    log.info("поехали: 1 ассистент + %s рабочих", len(workers))

    try:
        # Очередь разбирается отдельной корутиной, а не внутри обработчика
        # сообщения: пока обработчик ждёт получасовой прогон, aiogram не
        # берёт следующие апдейты — и вторая просьба человека доходит до
        # завода после того, как первая закончилась.
        await asyncio.gather(
            dp_assistant.start_polling(registry.bot("assistant"),
                                       allowed_updates=ASSISTANT_UPDATES),
            dp_workers.start_polling(*workers,
                                     allowed_updates=WORKER_UPDATES),
            handlers.pump(),
        )
    finally:
        await registry.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("остановлено")
