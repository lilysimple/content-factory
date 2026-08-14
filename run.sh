#!/bin/zsh
# Запуск и перезапуск завода.
#
# Если агент launchd загружен, работаем через него: launchd держит ровно
# один процесс на Label и сам поднимает упавший. Запуск мимо агента даёт
# второй поллинг, и два инстанса глушат друг друга Conflict'ом — именно
# так это и ломалось.
cd "$(dirname "$0")"

LABEL="space.lily.content-factory"
UID_NUM="$(id -u)"

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo "агент launchd загружен, перезапускаю через него"
  launchctl kickstart -k "gui/$UID_NUM/$LABEL"
  sleep 5
  echo "инстансов: $(pgrep -f 'main.py' | wc -l | tr -d ' ')"
  launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid)" | head -2
  exit 0
fi

# Агента нет — запускаем вручную. Поставить на автозапуск:
#   ./deploy/install-agent.sh
#
# SIGTERM застрявший в переподключении aiogram инстанс не берёт, нужен -9.
if pids=$(pgrep -f "main.py"); then
  kill -9 $pids 2>/dev/null
  sleep 1
fi
if pgrep -f "main.py" >/dev/null; then
  echo "не удалось погасить старый инстанс, запуск отменён" >&2
  exit 1
fi
exec ./.venv/bin/python main.py 2>&1 | tee -a bot.log
