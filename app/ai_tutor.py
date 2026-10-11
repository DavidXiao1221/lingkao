"""Small server-only DeepSeek client. No provider response changes learning state."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import secrets
import threading
import urllib.error
import urllib.request


ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_RESPONSE_BYTES = 200_000
MAX_REPLY_CHARS = 6_000


def _lock_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkerLease:
    """An OS-held liveness lock: survives request timeout, never confuses reused PIDs."""

    def __init__(self, directory):
        self.guard = threading.Lock()
        self.handle = None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_hex(24)
        self.path = directory / (self.token + ".lock")
        handle = self.path.open("x+b")
        try:
            handle.write(b"0")
            handle.flush()
            _lock_file(handle)
        except BaseException:
            handle.close()
            self.path.unlink(missing_ok=True)
            raise
        self.handle = handle

    @staticmethod
    def alive(directory, token):
        # Database values never become arbitrary filesystem paths. Unverifiable
        # ownership remains occupied rather than risking another paid request.
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{48}", token):
            return True
        path = Path(directory) / (token + ".lock")
        try:
            handle = path.open("r+b")
        except FileNotFoundError:
            return False
        except OSError:
            return True
        with handle:
            try:
                _lock_file(handle)
            except OSError:
                return True
            _unlock_file(handle)
        # A dead process can leave a file; the released OS lock proves liveness,
        # not its filename, age, PID or a wall-clock heartbeat.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    def close(self):
        with self.guard:
            if self.handle is None:
                return
            handle, self.handle = self.handle, None
            try:
                _unlock_file(handle)
            finally:
                handle.close()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def __del__(self):
        if getattr(self, "handle", None) is not None:
            self.close()


class TutorError(Exception):
    """A deliberately sanitized error safe to show in the learning interface."""


@dataclass(frozen=True)
class TutorConfig:
    api_key: str = field(default="", repr=False)
    model: str = "deepseek-flash"
    participant_limit: int = 30
    daily_limit: int = 100
    concurrency: int = 2
    deadline_seconds: int = 45
    max_tokens: int = 1000
    min_interval_seconds: float = 4

    @property
    def enabled(self):
        return bool(self.api_key)


def load_config(root: Path, environ=None):
    """Read only known local settings; process environment takes precedence."""
    environment = os.environ if environ is None else environ
    names = {
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "LINGKAO_AI_PARTICIPANT_LIMIT",
        "LINGKAO_AI_DAILY_LIMIT", "LINGKAO_AI_CONCURRENCY", "LINGKAO_AI_TIMEOUT_SECONDS",
    }
    values = {}
    path = Path(root) / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in names:
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({key: environment[key] for key in names if key in environment})

    def integer(name, default, maximum):
        raw = values.get(name, str(default))
        try:
            number = int(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{name} 必须为正整数。") from None
        if not 1 <= number <= maximum:
            raise ValueError(f"{name} 必须在 1 到 {maximum} 之间。")
        return number

    return TutorConfig(
        api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
        model=values.get("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash",
        participant_limit=integer("LINGKAO_AI_PARTICIPANT_LIMIT", 30, 100_000),
        daily_limit=integer("LINGKAO_AI_DAILY_LIMIT", 100, 1_000_000),
        concurrency=integer("LINGKAO_AI_CONCURRENCY", 2, 16),
        deadline_seconds=integer("LINGKAO_AI_TIMEOUT_SECONDS", 45, 120),
    )


def complete(config: TutorConfig, messages):
    if not config.enabled:
        raise TutorError("尚未配置在线 AI，可继续做题和使用预设讲解。")
    body = {
        "model": config.model, "messages": messages, "stream": False,
        "max_tokens": config.max_tokens, "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + config.api_key,
                 "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=min(30, config.deadline_seconds)) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TutorError("AI 回复过长，未显示回复。请缩小问题范围后重试。")
        data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise TutorError("在线答疑暂时繁忙，请稍后重试。") from None
        raise TutorError("在线答疑暂时不可用。做题和预设讲解仍可使用。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TutorError("在线答疑未连通或超时。做题和预设讲解仍可使用。") from None
    except (ValueError, UnicodeError):
        raise TutorError("AI 服务返回格式异常，未显示回复。") from None
    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise TutorError("AI 服务返回格式异常，未显示回复。") from None
    if not isinstance(message, str) or not message.strip():
        raise TutorError("AI 服务返回空回复，未显示回复。")
    if len(message) > MAX_REPLY_CHARS:
        raise TutorError("AI 回复过长，未显示回复。请缩小问题范围后重试。")
    message = message.strip()
    if data["choices"][0].get("finish_reason") == "length":
        note = "\n\n回复达到长度上限，可继续追问。"
        message = message[:MAX_REPLY_CHARS - len(note)] + note
    raw_usage = data.get("usage", {})
    usage = {}
    if isinstance(raw_usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(key)
            if type(value) is int and value >= 0:
                usage[key] = value
    return message, usage
