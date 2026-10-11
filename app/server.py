"""Loopback-only learning service with optional, asynchronous DeepSeek tutoring."""
from __future__ import annotations

import argparse
import datetime as dt
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

import ai_tutor
import engine
from engine import (
    ROOT, VERSION, ActionError, action, export_state, new_state, preview,
    public,
)

COOKIE_NAME = "lingkao_alpha_session"
MAX_BODY_BYTES = 16_000
POSTS_PER_MINUTE = 240
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/views.js": ("views.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
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
        self.owner_directory = self.path.parent / ("." + self.path.name + ".ai-workers")
        self.ai_owner = None
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_participants "
                "(participant TEXT PRIMARY KEY, calls INTEGER NOT NULL, last_started REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_daily (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_reservations "
                "(id TEXT PRIMARY KEY, deadline REAL NOT NULL, finished INTEGER NOT NULL DEFAULT 0, owner_token TEXT)"
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_reservations)")}
            if "owner_token" not in columns:
                connection.execute("ALTER TABLE ai_reservations ADD COLUMN owner_token TEXT")

    def close(self):
        with self.guard:
            if self.ai_owner is not None:
                self.ai_owner.close()
                self.ai_owner = None

    def owner_token(self):
        with self.guard:
            if self.ai_owner is None:
                self.ai_owner = ai_tutor.WorkerLease(self.owner_directory)
            return self.ai_owner.token

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

    def current_snapshot(self, sid):
        """Apply only migrations/expired-job cleanup, with cross-process CAS retries."""
        for _ in range(4):
            state, snapshot = self.load_snapshot(sid)
            if state is None:
                return state, snapshot
            changed = engine.migrate_state(state)
            changed = engine.expire_ai(state) or changed
            if not changed:
                return state, snapshot
            try:
                self.save(sid, state, expected_snapshot=snapshot)
                return state, json.dumps(state, ensure_ascii=False)
            except ActionError as exc:
                if exc.status != 409:
                    raise
        raise ActionError("学习进度正在更新，请稍后刷新。", status=409)

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

    @staticmethod
    def utc_day():
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def ai_limits(self, participant, config):
        with self.connect() as connection:
            row = connection.execute("SELECT calls FROM ai_participants WHERE participant=?", (participant,)).fetchone()
            daily = connection.execute("SELECT calls FROM ai_daily WHERE day=?", (self.utc_day(),)).fetchone()
        return {"participant_used": row[0] if row else 0, "participant_limit": config.participant_limit,
                "daily_used": daily[0] if daily else 0, "daily_limit": config.daily_limit}

    def reserve_ai(self, sid, state, *, expected_snapshot, request_id, deadline, config):
        """Durable quota reservation and pending state are one atomic CAS transaction."""
        stamp, day = time.time(), self.utc_day()
        participant = state["participant"]
        owner_token = self.owner_token()
        encoded = json.dumps(state, ensure_ascii=False)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT calls,last_started FROM ai_participants WHERE participant=?", (participant,)
            ).fetchone()
            used, last_started = row if row else (0, 0)
            daily = connection.execute("SELECT calls FROM ai_daily WHERE day=?", (day,)).fetchone()
            daily_used = daily[0] if daily else 0
            if used >= config.participant_limit:
                raise ActionError("本次学习的在线答疑次数已用完，可继续做题和查看预设讲解。", 429)
            if daily_used >= config.daily_limit:
                raise ActionError("本机今日在线答疑次数已用完，可继续做题和查看预设讲解。", 429)
            if stamp - last_started < config.min_interval_seconds:
                raise ActionError("提问过于频繁，请稍后再试。", 429)
            active = 0
            for reservation, owner, old_deadline in connection.execute(
                    "SELECT id,owner_token,deadline FROM ai_reservations WHERE finished=0").fetchall():
                # Old installations have no owner marker: retain their original
                # lease until its deadline. Stop old service versions on upgrade.
                alive = ai_tutor.WorkerLease.alive(self.owner_directory, owner) if owner else old_deadline > stamp
                if alive:
                    active += 1
                else:
                    connection.execute("UPDATE ai_reservations SET finished=1 WHERE id=?", (reservation,))
            if active >= config.concurrency:
                raise ActionError("在线答疑正忙，请稍后重试。", 503)
            updated = connection.execute(
                "UPDATE sessions SET data=? WHERE id=? AND data=?", (encoded, sid, expected_snapshot)
            )
            if updated.rowcount != 1:
                raise ActionError("学习进度已经更新，本次提问未发送。请刷新后重试。", 409)
            connection.execute(
                "INSERT INTO ai_participants VALUES (?,?,?) ON CONFLICT(participant) "
                "DO UPDATE SET calls=excluded.calls,last_started=excluded.last_started", (participant, used + 1, stamp)
            )
            connection.execute(
                "INSERT INTO ai_daily VALUES (?,?) ON CONFLICT(day) DO UPDATE SET calls=excluded.calls", (day, daily_used + 1)
            )
            connection.execute("INSERT INTO ai_reservations(id,deadline,owner_token) VALUES (?,?,?)", (request_id, deadline, owner_token))
            connection.execute("DELETE FROM ai_reservations WHERE finished=1 AND deadline<?", (stamp - 86_400,))

    def release_ai(self, request_id):
        with self.connect() as connection:
            connection.execute("UPDATE ai_reservations SET finished=1 WHERE id=?", (request_id,))

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

    def __init__(self, port=8000, db_path=None, *, ai_config=None, ai_client=None):
        path = db_path or os.environ.get("LINGKAO_DB_PATH") or ROOT / "data" / "lingkao-alpha.sqlite3"
        self.store = SessionStore(path)
        self.ai_config = ai_config if ai_config is not None else ai_tutor.load_config(ROOT)
        self.ai_client = ai_client if ai_client is not None else ai_tutor.complete
        self.ai_slots = threading.BoundedSemaphore(self.ai_config.concurrency)
        self.ai_workers = set()
        self.ai_guard = threading.Lock()
        self.ai_closing = False
        self.ai_preparing = 0
        super().__init__(("127.0.0.1", port), Handler)

    def public_state(self, state):
        result = public(state)
        result["ai_enabled"] = self.ai_config.enabled
        result["ai_limits"] = self.store.ai_limits(state["participant"], self.ai_config)
        return result

    def start_tutor(self, sid, state, snapshot, payload):
        with self.ai_guard:
            if self.ai_closing:
                raise ActionError("服务正在关闭，请重新启动后提问。", 503)
            self.ai_preparing += 1
        try:
            return self._start_tutor(sid, state, snapshot, payload)
        finally:
            with self.ai_guard:
                self.ai_preparing -= 1
                if self.ai_closing and not self.ai_workers and not self.ai_preparing:
                    self.store.close()

    def _start_tutor(self, sid, state, snapshot, payload):
        messages = engine.prepare_ai(state, payload)
        if not self.ai_config.enabled:
            raise ActionError("尚未配置在线 AI，可继续做题和使用预设讲解。", 503)
        if not self.ai_slots.acquire(blocking=False):
            raise ActionError("在线答疑正忙，请稍后重试。", 503)
        request_id = secrets.token_urlsafe(24)
        deadline = time.time() + self.ai_config.deadline_seconds
        try:
            engine.begin_ai(state, payload, request_id, deadline)
            # Validate public serialization before committing any charge/pending state.
            json.dumps(public(state), ensure_ascii=False)
            self.store.reserve_ai(sid, state, expected_snapshot=snapshot,
                                  request_id=request_id, deadline=deadline, config=self.ai_config)
        except Exception:
            self.ai_slots.release()
            raise
        identity = (state["participant"], state.get("course_version"))
        worker = threading.Thread(target=self._run_tutor,
                                  args=(sid, request_id, identity, messages), daemon=True)
        with self.ai_guard:
            self.ai_workers.add(worker)
        try:
            worker.start()
        except RuntimeError:
            with self.ai_guard:
                self.ai_workers.discard(worker)
            self._finish_tutor(sid, request_id, identity, error="在线答疑暂时无法启动，请稍后重试。")
            self.store.release_ai(request_id)
            self.ai_slots.release()
            state, _ = self.store.current_snapshot(sid)
        return self.public_state(state)

    def _finish_tutor(self, sid, request_id, identity, **result):
        with self.store.lock(sid):
            for _ in range(4):
                state, snapshot = self.store.current_snapshot(sid)
                if state is None or (state["participant"], state.get("course_version")) != identity:
                    return
                if not engine.finish_ai(state, request_id, **result):
                    return
                try:
                    self.store.save(sid, state, expected_snapshot=snapshot)
                    return
                except ActionError as exc:
                    if exc.status != 409:
                        raise

    def _run_tutor(self, sid, request_id, identity, messages):
        try:
            try:
                reply, usage = self.ai_client(self.ai_config, messages)
                result = {"reply": reply, "usage": usage}
            except ai_tutor.TutorError as exc:
                result = {"error": str(exc)}
            except Exception:
                # Neither an exception string nor a provider body may expose secrets.
                result = {"error": "在线答疑暂时不可用，请稍后重试。"}
            self._finish_tutor(sid, request_id, identity, **result)
        except Exception as exc:
            print("[AI ERROR]", type(exc).__name__)
        finally:
            try:
                self.store.release_ai(request_id)
            except Exception as exc:
                print("[AI CLEANUP]", type(exc).__name__)
            self.ai_slots.release()
            with self.ai_guard:
                self.ai_workers.discard(threading.current_thread())
                if self.ai_closing and not self.ai_workers and not self.ai_preparing:
                    self.store.close()

    def server_close(self):
        with self.ai_guard:
            self.ai_closing = True
            if not self.ai_workers and not self.ai_preparing:
                self.store.close()
        super().server_close()


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
            return self.respond(200, {"ok": True, "version": VERSION,
                                     "ai_enabled": self.server.ai_config.enabled, "local_only": True})
        if path not in ("/api/state", "/api/export", "/api/catalog"):
            return self.respond(404, {"error": "页面不存在。"})
        store = self.server.store
        sid = store.existing_sid(self.headers.get("Cookie"))
        fresh = False
        if sid is None:
            if path != "/api/state":
                return self.respond(403, {"error": "请先打开学习页面。"})
            sid, fresh = store.create(), True
        with store.lock(sid):
            try:
                state, _ = store.current_snapshot(sid)
                if state is None:
                    if path != "/api/state":
                        return self.respond(403, {"error": "会话不存在，请刷新页面。"})
                    sid, fresh = store.create(), True
                    state, _ = store.current_snapshot(sid)
                if path == "/api/state":
                    data = self.server.public_state(state)
                elif path == "/api/catalog":
                    data = engine.catalog(state)
                else:
                    data = export_state(state)
                return self.respond(200, data, cookie=sid if fresh else None)
            except ActionError as exc:
                return self.respond(exc.status, {"error": str(exc)})
            except Exception as exc:
                print("[ERROR]", type(exc).__name__)
                return self.respond(500, {"error": "本地服务暂时无法读取学习进度，请稍后刷新。"})

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
            try:
                state, snapshot = store.current_snapshot(sid)
            except ActionError as exc:
                return self.respond(exc.status, {"error": str(exc)})
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
                    if (state.get("ai_request") or {}).get("status") == "pending":
                        raise ActionError("请先取消或等待当前 AI 答疑，再清除学习记录。", status=409)
                    revision = state["revision"] + 1
                    state = new_state()
                    state["revision"] = revision
                elif self.path == "/api/action":
                    action(state, payload)
                else:
                    result = self.server.start_tutor(sid, state, snapshot, payload)
                    return self.respond(202, result)
                # Serialize the response first: errors must never persist partial mutations.
                result = self.server.public_state(state)
                json.dumps(result, ensure_ascii=False)
                store.save(sid, state, expected_snapshot=snapshot)
                return self.respond(200, result)
            except ActionError as exc:
                return self.respond(exc.status, {"error": str(exc)})
            except Exception as exc:
                print("[ERROR]", type(exc).__name__)
                return self.respond(500, {"error": "本地服务暂时无法返回结果，请刷新查看当前进度。"})


def main():
    parser = argparse.ArgumentParser(description="临考题单与练习本地服务")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须为1—65535。")
    try:
        server = LocalServer(port=args.port)
    except ValueError as exc:
        parser.exit(1, f"本地服务配置无效：{exc}\n")
    except OSError:
        parser.exit(1, "本地服务启动失败。若端口已被占用，可使用 --port 8001；请检查文件读取权限。\n")
    print(f"临考 {VERSION}: http://127.0.0.1:{args.port}")
    print("模式：" + ("DeepSeek 在线答疑（密钥仅在服务端）。" if server.ai_config.enabled
                     else "离线训练与预设讲解（未配置在线 AI）。"))
    print("此窗口关闭后服务停止；再次启动可恢复当前浏览器学习记录。Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
