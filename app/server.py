"""Loopback-only HTTP service for the offline Lingkao learning pilot."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hmac
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from engine import (
    ROOT, VERSION, ActionError, action, export_state, new_state, preview,
    public, tutor,
)

COOKIE_NAME = "lingkao_alpha_session"
MAX_BODY_BYTES = 16_000
POSTS_PER_MINUTE = 240
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
POST_PATHS = frozenset(("/api/action", "/api/tutor", "/api/preview", "/api/reset"))


class SessionStore:
    """Local locks serialize requests; SQLite snapshot checks protect other processes."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.guard = threading.Lock()
        self.locks = {}
        self.rates = {}
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def lock(self, sid):
        with self.guard:
            return self.locks.setdefault(sid, threading.RLock())

    def load(self, sid):
        return self.load_snapshot(sid)[0]

    def load_snapshot(self, sid):
        with self.connect() as connection:
            row = connection.execute("SELECT data FROM sessions WHERE id = ?", (sid,)).fetchone()
        return (json.loads(row[0]), row[0]) if row else (None, None)

    def save(self, sid, state, *, expected_snapshot):
        encoded = json.dumps(state, ensure_ascii=False)
        with self.connect() as connection:
            # Compare the exact stored bytes, including legacy JSON formatting. Do not
            # hold a database transaction open while the math worker is running.
            updated = connection.execute(
                "UPDATE sessions SET data = ? WHERE id = ? AND data = ?",
                (encoded, sid, expected_snapshot),
            )
            if updated.rowcount != 1:
                raise ActionError("学习进度已经更新，本次操作未记录。请刷新后重试。", status=409)

    def existing_sid(self, header):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(header or "")
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        sid = morsel.value if morsel else ""
        return sid if re.fullmatch(r"[A-Za-z0-9_-]{43}", sid) else None

    def create(self):
        sid = secrets.token_urlsafe(32)
        state = new_state()
        with self.lock(sid):
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?)",
                    (sid, json.dumps(state, ensure_ascii=False)),
                )
        return sid

    def allow_post(self, sid):
        # Called under the session lock; previews are bounded too.
        now = time.monotonic()
        stamps = self.rates.setdefault(sid, deque())
        while stamps and stamps[0] <= now - 60:
            stamps.popleft()
        if len(stamps) >= POSTS_PER_MINUTE:
            return False
        stamps.append(now)
        return True


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=8000, db_path=None):
        path = db_path or os.environ.get("LINGKAO_DB_PATH") or ROOT / "data" / "lingkao-alpha.sqlite3"
        self.store = SessionStore(path)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = f"LingkaoLocal/{VERSION}"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, fmt, *args):
        # Do not include request bodies, cookies, query strings or student answers.
        if len(args) >= 2:
            print("[HTTP]", self.command, self.path.split("?", 1)[0], args[1])

    def respond(self, code, data, content_type="application/json; charset=utf-8", cookie=None):
        if code >= 400 and self.command == "POST":
            self.discard_small_body()
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800")
        try:
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, TimeoutError):
            # Cancelling a preview can close the browser socket during either write.
            # The operation has already completed; do not send a second 500 response.
            self.close_connection = True

    def discard_small_body(self):
        # Closing a Windows socket with unread bytes can discard the error response.
        # Drain only a small declared body and never wait indefinitely on rejected input.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        remaining = length - getattr(self, "body_consumed", 0)
        if 0 < remaining <= MAX_BODY_BYTES * 4:
            try:
                self.connection.settimeout(0.5)
                self.body_consumed = length
                self.rfile.read(remaining)
            except (TimeoutError, OSError):
                pass
            finally:
                self.connection.settimeout(10)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "仅允许本机访问。"})
        path = self.path.split("?", 1)[0]
        if path in ASSETS:
            name, kind = ASSETS[path]
            try:
                data = (ROOT / "web" / name).read_bytes()
            except FileNotFoundError:
                return self.respond(404, {"error": "页面资源不存在。"})
            return self.respond(200, data, kind)
        if path == "/favicon.ico":
            return self.respond(204, b"", "image/x-icon")
        if path == "/api/health":
            return self.respond(200, {"ok": True, "version": VERSION, "ai_enabled": False, "local_only": True})
        if path not in ("/api/state", "/api/export"):
            return self.respond(404, {"error": "页面不存在。"})
        store = self.server.store
        sid = store.existing_sid(self.headers.get("Cookie"))
        fresh = False
        if sid is None:
            if path == "/api/export":
                return self.respond(403, {"error": "请先打开学习页面。"})
            sid, fresh = store.create(), True
        with store.lock(sid):
            state = store.load(sid)
            if state is None:
                if path == "/api/export":
                    return self.respond(403, {"error": "会话不存在，请刷新页面。"})
                sid, fresh = store.create(), True
                # A newly generated session is not observable until its cookie is sent.
                state = store.load(sid)
            try:
                data = public(state) if path == "/api/state" else export_state(state)
                return self.respond(200, data, cookie=sid if fresh else None)
            except ActionError as exc:
                return self.respond(exc.status, {"error": str(exc)})

    def do_POST(self):
        if not self.valid_host():
            return self.respond(403, {"error": "仅允许本机访问。"})
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self.respond(403, {"error": "不接受跨站请求。"})
        if self.path not in POST_PATHS:
            return self.respond(404, {"error": "接口不存在。"})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error": "不支持分块请求。"})
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1:
                raise ValueError
            length = int(lengths[0])
        except ValueError:
            return self.respond(400, {"error": "请求长度无效。"})
        if not 0 < length <= MAX_BODY_BYTES:
            return self.respond(413, {"error": "请求体为空或过大。"})
        if self.headers.get_content_type() != "application/json":
            return self.respond(415, {"error": "只接受JSON。"})
        try:
            raw = self.rfile.read(length)
            self.body_consumed = len(raw)
            if len(raw) != length:
                raise ValueError
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError):
            return self.respond(400, {"error": "JSON格式错误。"})
        except (TimeoutError, OSError):
            return self.respond(408, {"error": "读取请求超时，请重试。"})
        store = self.server.store
        sid = store.existing_sid(self.headers.get("Cookie"))
        if sid is None:
            return self.respond(403, {"error": "会话校验失败，请刷新页面。"})
        with store.lock(sid):
            state, snapshot = store.load_snapshot(sid)
            # Check the freshly loaded token inside the lock, including for resets.
            token = self.headers.get("X-CSRF-Token", "")
            if state is None or not hmac.compare_digest(token.encode("utf-8"), state["csrf"].encode("utf-8")):
                return self.respond(403, {"error": "会话校验失败，请刷新页面。"})
            if not store.allow_post(sid):
                return self.respond(429, {"error": "操作过于频繁，请稍后重试。"})
            try:
                if self.path == "/api/preview":
                    return self.respond(200, preview(state, payload))
                if self.path == "/api/reset":
                    if type(payload.get("revision")) is not int or payload["revision"] != state["revision"]:
                        raise ActionError("页面记录已更新，请刷新后再操作。", status=409)
                    revision = state["revision"] + 1
                    state = new_state()
                    state["revision"] = revision
                elif self.path == "/api/action":
                    action(state, payload)
                else:
                    tutor(state, payload)
                # Serialize the response first: errors must never persist partial mutations.
                result = public(state)
                json.dumps(result, ensure_ascii=False)
                store.save(sid, state, expected_snapshot=snapshot)
                return self.respond(200, result)
            except ActionError as exc:
                return self.respond(exc.status, {"error": str(exc)})
            except Exception as exc:
                print("[ERROR]", type(exc).__name__)
                return self.respond(500, {"error": "本地服务出现异常，当前操作未保存，请刷新后重试。"})


def main():
    parser = argparse.ArgumentParser(description="临考离线训练本地服务")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须为1—65535。")
    try:
        server = LocalServer(port=args.port)
    except OSError as exc:
        parser.exit(1, f"本地服务启动失败：{exc}\n若端口已被占用，可使用 --port 8001。\n")
    print(f"临考 {VERSION}: http://127.0.0.1:{args.port}")
    print("模式：离线训练与预设讲解（无在线AI调用）。")
    print("此窗口关闭后服务停止；再次启动可恢复当前浏览器学习记录。Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
