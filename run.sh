#!/bin/zsh
# Запуск завода независимо от сессии Claude.
# Только один экземпляр: Telegram разрешает один опрос на бота.
cd "$(dirname "$0")"
# SIGTERM застрявший в переподключении aiogram инстанс не берёт, нужен -9.
# Без проверки после гашения инстансы копятся и глушат друг друга.
if pids=$(pgrep -f "main.py"); then
  kill -9 $pids 2>/dev/null
  sleep 1
fi
if pgrep -f "main.py" >/dev/null; then
  echo "не удалось погасить старый инстанс, запуск отменён" >&2
  exit 1
fi
exec ./.venv/bin/python main.py 2>&1 | tee -a bot.log
