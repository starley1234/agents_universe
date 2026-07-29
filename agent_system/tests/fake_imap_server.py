#!/usr/bin/env python3
"""Минимальный поддельный IMAP-сервер для тестов: настоящий текстовый
протокол IMAP4 на сыром сокете, фиктивные данные — как fake_mcp_server.py
для MCP. Поддерживает ровно то подмножество команд, которое использует
agent/tools/messaging.py (LOGIN, SELECT/EXAMINE, SEARCH, FETCH, LOGOUT),
плюс CAPABILITY, которую imaplib запрашивает при подключении.

Письма задаются заранее (см. MESSAGES) — по одному RFC822-блобу на номер.
"""
from __future__ import annotations

import socket
import threading

MESSAGES: dict[int, bytes] = {
    1: (b"From: sender1@example.com\r\n"
       b"To: bot@example.com\r\n"
       b"Subject: =?utf-8?b?0J/QtdGA0LLQvtC1?=\r\n"  # "Первое" в MIME encoded-word
       b"Date: Mon, 01 Jan 2024 10:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
       b"\xd0\xa2\xd0\xb5\xd0\xba\xd1\x81\xd1\x82 \xd0\xbf\xd0\xb5\xd1\x80\xd0\xb2\xd0\xbe\xd0\xb3\xd0\xbe \xd0\xbf\xd0\xb8\xd1\x81\xd1\x8c\xd0\xbc\xd0\xb0\r\n"),
    2: (b"From: sender2@example.com\r\n"
       b"To: bot@example.com\r\n"
       b"Subject: Second\r\n"
       b"Date: Tue, 02 Jan 2024 11:00:00 +0000\r\n"
       b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Plain second message body.\r\n"),
}


class _Conn(threading.Thread):
    def __init__(self, conn: socket.socket) -> None:
        super().__init__(daemon=True)
        self.conn = conn

    def run(self) -> None:
        f = self.conn.makefile("rwb")
        try:
            f.write(b"* OK IMAP4rev1 fake ready\r\n")
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    break
                self._handle(f, line.decode("utf-8", "replace").strip())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.conn.close()

    def _handle(self, f, line: str) -> None:
        parts = line.split(" ", 2)
        tag = parts[0] if parts else "*"
        cmd = parts[1].upper() if len(parts) > 1 else ""

        if cmd == "CAPABILITY":
            f.write(b"* CAPABILITY IMAP4rev1\r\n")
            f.write(f"{tag} OK CAPABILITY completed\r\n".encode())
        elif cmd == "LOGIN":
            f.write(f"{tag} OK LOGIN completed\r\n".encode())
        elif cmd in ("SELECT", "EXAMINE"):
            f.write(f"* {len(MESSAGES)} EXISTS\r\n* 0 RECENT\r\n".encode())
            f.write(f"{tag} OK [READ-ONLY] {cmd} completed\r\n".encode())
        elif cmd == "SEARCH":
            ids = " ".join(str(i) for i in sorted(MESSAGES))
            f.write(f"* SEARCH {ids}\r\n".encode())
            f.write(f"{tag} OK SEARCH completed\r\n".encode())
        elif cmd == "FETCH":
            rest = parts[2] if len(parts) > 2 else ""
            try:
                num = int(rest.split()[0])
            except (ValueError, IndexError):
                num = 1
            raw = MESSAGES.get(num, b"")
            if "HEADER.FIELDS" in rest:
                # разбираем только заголовки (без пустой строки-разделителя)
                head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                body = head
            else:
                body = raw
            f.write(f"* {num} FETCH (BODY[...] {{{len(body)}}}\r\n".encode())
            f.write(body)
            f.write(b")\r\n")
            f.write(f"{tag} OK FETCH completed\r\n".encode())
        elif cmd == "LOGOUT":
            f.write(b"* BYE logging out\r\n")
            f.write(f"{tag} OK LOGOUT completed\r\n".encode())
            f.flush()
            return
        elif cmd == "CLOSE":
            f.write(f"{tag} OK CLOSE completed\r\n".encode())
        else:
            f.write(f"{tag} BAD unknown command\r\n".encode())
        f.flush()


class FakeImapServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            _Conn(conn).start()

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
