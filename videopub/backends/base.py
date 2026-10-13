"""后端抽象基类。

后端负责「把一条 VideoItem 真正送到视频号」。不同通道（演练 / 开放接口 /
网页自动化）实现同一接口，发布器无需关心差异。

契约：
  * :meth:`authenticate` 在发布前调用一次，失败抛 :class:`AuthError`；
  * :meth:`publish` 返回 :class:`PublishResult`，**不要**自行吞掉异常——
    应交由发布器统一重试与记录；若确实要表达失败，抛对应异常即可；
  * :meth:`healthcheck` 返回后端可用性信息（不抛异常）。
"""

from __future__ import annotations

import abc
import time
from typing import Any

from ..config import Config
from ..models import PublishResult, VideoItem


class PublishBackend(abc.ABC):
    name: str = "base"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._authenticated = False
        self._auth_info: dict[str, Any] = {}

    # -- 生命周期 ----------------------------------------------------------
    def authenticate(self) -> dict[str, Any]:
        """建立 / 校验凭证。默认无操作。"""
        self._authenticated = True
        return self._auth_info

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    # -- 发布 --------------------------------------------------------------
    @abc.abstractmethod
    def publish(self, item: VideoItem, *, options: dict[str, Any] | None = None) -> PublishResult:
        """执行一次发布。实现方需填充 result 的关键字段。"""

    # -- 诊断 --------------------------------------------------------------
    def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True, "detail": "no-op healthcheck"}

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "class": type(self).__name__,
            "authenticated": self._authenticated,
            "auth": {k: ("***" if "token" in k.lower() or "secret" in k.lower() else v)
                     for k, v in self._auth_info.items()},
        }

    # -- 工具 --------------------------------------------------------------
    @staticmethod
    def _result(item: VideoItem, *, options: dict[str, Any] | None = None) -> PublishResult:
        opts = options or {}
        return PublishResult(
            item_id=item.item_id,
            title=item.title,
            backend=opts.get("backend_name", "base"),
            trigger=opts.get("trigger", "manual"),
            batch_id=opts.get("batch_id", ""),
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


class RetryableTimer:
    """退避计时器（供后端在轮询远端处理状态时复用）。"""

    def __init__(self, base: float = 1.0, factor: float = 2.0, max_delay: float = 30.0) -> None:
        self.base = base
        self.factor = factor
        self.max_delay = max_delay

    def sleep(self, attempt: int) -> None:
        delay = min(self.base * (self.factor ** max(attempt - 1, 0)), self.max_delay)
        if delay > 0:
            time.sleep(delay)
