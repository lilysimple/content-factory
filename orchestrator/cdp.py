"""Разговор с Chrome по протоколу отладки. Минимальный, на стандартной библиотеке.

Заведено ради сборщика профилей Instagram: страница отдаётся пустой
оболочкой, данные приезжают запросом из скрипта, и снять их можно только
изнутри страницы — с её куками и заголовками. `--dump-dom` тут не
годится: он снимает разметку, а не ответ `api/v1`, и вдобавок требует
headless, который Instagram узнаёт и показывает стену входа.

Почему свой клиент, а не библиотека: WebSocket нужен ровно в одном
сценарии и в одну сторону — послать команду, дождаться ответа с тем же
`id`. Это восемьдесят строк рукопожатия и разбора кадров против новой
зависимости в проекте, где их четыре штуки. Ни сжатия, ни серверных
пингов с ответом, ни фрагментации больше двух кадров тут не нужно.

Читает **только то, что видит человек в своём браузере**: свой профиль
Chrome, свои куки, публичные страницы. Вход за кого-то другого и обход
защиты сюда не входят.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
from urllib.request import urlopen

log = logging.getLogger("cdp")

TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class CDPError(RuntimeError):
    """Chrome не ответил или ответил ошибкой."""


def targets(port: int, timeout: float = 5.0) -> list[dict]:
    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def page_socket(port: int, *, timeout: float = 5.0) -> str:
    """Адрес WebSocket первой вкладки. Вкладок нет — это ошибка, а не пусто."""
    for t in targets(port, timeout):
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise CDPError("у Chrome нет ни одной вкладки для отладки")


class Socket:
    """WebSocket-клиент на один разговор. Только текстовые кадры."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        rest = url.removeprefix("ws://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._handshake(hostport, "/" + path)
        self._id = 0

    def _handshake(self, hostport: str, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        self.buf = rest
        if b"101" not in head.split(b"\r\n")[0]:
            raise CDPError(f"Chrome не поднял WebSocket: {head[:80]!r}")

    def _fill(self) -> None:
        # Chrome рвёт сокет молча, когда вкладка меняет процесс на
        # переходе между сайтами. Для читателя это одна ошибка, а не
        # выбор из `OSError` и `CDPError`: переподключиться он умеет.
        try:
            chunk = self.sock.recv(65536)
        except OSError as e:
            raise CDPError(f"связь с Chrome оборвалась: {type(e).__name__}") from e
        if not chunk:
            raise CDPError("Chrome закрыл соединение")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, payload: bytes, opcode: int = TEXT) -> None:
        # Клиент обязан маскировать кадры — иначе Chrome рвёт соединение.
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + masked)
        except OSError as e:
            raise CDPError(f"Chrome не принял команду: {type(e).__name__}") from e

    def _recv(self) -> tuple[int, bytes]:
        b0, b1 = self._take(2)
        opcode, n = b0 & 0x0F, b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._take(8))[0]
        return opcode, self._take(n)

    def call(self, method: str, params: dict | None = None) -> dict:
        """Команда и ответ на неё. События по дороге пропускаются.

        Ответы приходят вперемешку с событиями страницы, и совпадение по
        `id` тут не удобство, а условие: без него `Runtime.evaluate`
        вернул бы первое попавшееся `Page.frameNavigated`.
        """
        self._id += 1
        want = self._id
        self._send(json.dumps({"id": want, "method": method,
                               "params": params or {}}).encode())
        while True:
            opcode, payload = self._recv()
            if opcode == CLOSE:
                raise CDPError(f"Chrome закрыл вкладку на {method}")
            if opcode == PING:
                self._send(payload, PONG)
                continue
            if opcode not in (TEXT, BINARY):
                continue
            msg = json.loads(payload.decode("utf-8", "replace"))
            if msg.get("id") != want:
                continue                      # событие, не наш ответ
            if err := msg.get("error"):
                raise CDPError(f"{method}: {err.get('message')}")
            return msg.get("result") or {}

    def evaluate(self, expression: str, *, timeout_ms: int = 30_000):
        """Выполнить JS на странице и дождаться промиса. Вернуть значение."""
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": timeout_ms,
        })
        if thrown := res.get("exceptionDetails"):
            raise CDPError(thrown.get("text") or "исключение на странице")
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self._send(b"", CLOSE)
        except OSError:
            pass
        self.sock.close()

    def __enter__(self) -> "Socket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
