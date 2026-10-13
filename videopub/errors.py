"""异常体系.

设计原则：
  * 所有模块内异常继承 :class:`PublisherError`，便于 CLI 与调度器统一兜底。
  * 每个异常携带 ``code``（稳定错误码，用于日志检索与结果查询）、``detail``（结构化补充）。
  * ``retryable`` 标记该错误是否值得重试（调度器与发布器据此决定退避重试）。
"""

from __future__ import annotations

from typing import Any


class PublisherError(Exception):
    """模块内所有异常的基类。"""

    code: str = "EPUB"
    retryable: bool = False

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None,
                 retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}
        # 允许调用方按场景覆盖类级默认值。
        # 典型用途：网页自动化里「定时时间没设上」这种**确定性**失败，
        # 重试只会白白把视频再传一遍，因此显式标成不可重试。
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------------------- #
# 配置 / 环境
# --------------------------------------------------------------------------- #
class ConfigError(PublisherError):
    code = "ECONFIG"


class StateError(PublisherError):
    code = "ESTATE"


# --------------------------------------------------------------------------- #
# 内容源扫描与解析
# --------------------------------------------------------------------------- #
class ScanError(PublisherError):
    code = "ESCAN"


class ContentParseError(PublisherError):
    """发布文案解析失败（通常可降级：用目录名兜底）。"""

    code = "ECONTENT"


# --------------------------------------------------------------------------- #
# 内容校验
# --------------------------------------------------------------------------- #
class ValidationError(PublisherError):
    code = "EVALID"


# --------------------------------------------------------------------------- #
# 后端 / 网络
# --------------------------------------------------------------------------- #
class BackendError(PublisherError):
    code = "EBACKEND"


class AuthError(BackendError):
    """登录态失效 / 凭证错误 —— 需要人工介入，因此不自动重试。"""

    code = "EAUTH"
    retryable = False


class UploadError(BackendError):
    code = "EUPLOAD"
    retryable = True


class RateLimitError(BackendError):
    code = "ERATELIMIT"
    retryable = True


class RemoteRejectError(BackendError):
    """服务端明确拒绝（内容审核不通过等）—— 重试无意义。"""

    code = "EREJECT"
    retryable = False


class DependencyError(BackendError):
    """可选的第三方依赖缺失。"""

    code = "EDEPENDENCY"
    retryable = False


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
class ScheduleError(PublisherError):
    code = "ESCHEDULE"


# 兼容别名
PublishError = PublisherError
