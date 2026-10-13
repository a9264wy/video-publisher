"""后端注册表。

新增后端只需实现 :class:`PublishBackend` 并在此登记。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..errors import BackendError
from .base import PublishBackend, RetryableTimer
from .channels_web import ChannelsWebBackend
from .dryrun import DryRunBackend
from .openapi import OpenApiBackend

REGISTRY: dict[str, type[PublishBackend]] = {
    "dryrun": DryRunBackend,
    "openapi": OpenApiBackend,
    "channels_web": ChannelsWebBackend,
}


def create_backend(name: str, config: Config, **kwargs: Any) -> PublishBackend:
    """按名称实例化后端。"""
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise BackendError(
            f"未知后端: {name!r}",
            detail={"available": sorted(REGISTRY)},
        )
    klass = REGISTRY[key]
    try:
        return klass(config, **kwargs)
    except TypeError:
        # 后端未接受额外参数时忽略
        return klass(config)


def list_backends() -> list[dict[str, str]]:
    return [
        {"name": "dryrun", "desc": "演练后端：不联网，完整模拟发布流程（默认，安全）"},
        {"name": "openapi", "desc": "开放接口后端：调用可配置的视频号/网关 HTTP 接口"},
        {"name": "channels_web", "desc": "视频号助手网页自动化（需 playwright，支持扫码登录）"},
    ]


__all__ = [
    "PublishBackend",
    "DryRunBackend",
    "OpenApiBackend",
    "ChannelsWebBackend",
    "RetryableTimer",
    "REGISTRY",
    "create_backend",
    "list_backends",
]
