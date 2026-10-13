"""日志体系。

三路输出：
  * 控制台     —— 人读
  * logs/videopub.log —— 轮转文本日志（含行号、线程）
  * logs/publish.jsonl —— 结构化 JSONL，供「发布日志查询」与报表消费

所有 logger 统一由 :func:`setup_logging` 初始化，可重复调用（幂等）。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Any

_CONFIGURED = False
_LOCK = threading.Lock()
_JSONL_PATH: Path | None = None

TEXT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_data", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    log_dir: str | Path = "logs",
    *,
    level: int = logging.INFO,
    console: bool = True,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 7,
) -> logging.Logger:
    """初始化日志，幂等。返回根 logger。"""
    global _CONFIGURED, _JSONL_PATH
    with _LOCK:
        root = logging.getLogger("videopub")
        if _CONFIGURED:
            return root
        root.setLevel(level)
        root.propagate = False

        d = Path(log_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 日志目录不可写时降级为仅控制台
            if console:
                print(f"[warn] 日志目录不可用({d}): {exc}，仅输出到控制台", file=sys.stderr)
            d = None  # type: ignore[assignment]

        if console:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setFormatter(logging.Formatter(TEXT_FORMAT, DATE_FORMAT))
            sh.setLevel(level)
            root.addHandler(sh)

        if d is not None:
            fh = logging.handlers.RotatingFileHandler(
                d / "videopub.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter(TEXT_FORMAT, DATE_FORMAT))
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)

            _JSONL_PATH = d / "publish.jsonl"
            jh = logging.handlers.RotatingFileHandler(
                _JSONL_PATH, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            jh.setFormatter(JsonlFormatter())
            jh.setLevel(level)
            jh.addFilter(lambda r: bool(getattr(r, "extra_data", None)))
            root.addHandler(jh)

        _CONFIGURED = True
        return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"videopub.{name}")


def shutdown_logging() -> None:
    """关闭并移除所有日志处理器，释放文件句柄。

    为什么必须有：Windows 上被打开的文件无法删除/移动，若进程结束后仍持有
    ``logs/publish.jsonl`` 句柄，用户将无法清理或迁移日志目录（实测踩过）。
    CLI 在退出前会调用本函数；调用后 :func:`setup_logging` 可重新初始化。
    """
    global _CONFIGURED, _JSONL_PATH
    with _LOCK:
        root = logging.getLogger("videopub")
        for handler in list(root.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # 关闭失败不应影响退出
                pass
            root.removeHandler(handler)
        _CONFIGURED = False
        _JSONL_PATH = None


def log_event(logger: logging.Logger, level: int, msg: str, **extra: Any) -> None:
    """结构化事件日志：写入文本日志的同时落一行 JSONL。"""
    logger.log(level, msg, extra={"extra_data": extra} if extra else None)


def jsonl_path() -> Path | None:
    return _JSONL_PATH


def read_jsonl(limit: int | None = None, level: str | None = None) -> list[dict[str, Any]]:
    """读取结构化发布日志（倒序，最新在前）。"""
    if _JSONL_PATH is None or not _JSONL_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with _JSONL_PATH.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if level and obj.get("level") != level:
                    continue
                out.append(obj)
    except OSError:
        return []
    out.reverse()
    return out[:limit] if limit else out
