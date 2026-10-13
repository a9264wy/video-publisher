"""videopub - 微信视频号内容发布模块.

零第三方依赖（核心基于标准库）。可选后端：
  - dryrun        : 演练后端，不联网，校验 + 模拟上传（默认，安全）
  - openapi       : 通用 HTTP 开放接口后端（urllib 实现，可对接视频号网关）
  - channels_web  : 视频号助手网页自动化（需可选依赖 playwright）

快速开始::

    from videopub import Config, VideoPublisher

    cfg = Config.load("config.json")
    pub = VideoPublisher(cfg)
    result = pub.publish_one(item)          # 发布单条
    results = pub.run(limit=3)              # 发布一批（待发布队列前 3 条）
"""

from .config import Config
from .errors import (
    AuthError,
    BackendError,
    ConfigError,
    ContentParseError,
    PublishError,
    PublisherError,
    RateLimitError,
    ScanError,
    StateError,
    UploadError,
    ValidationError,
)
from .models import PublishResult, PublishStatus, VideoItem
from .publisher import VideoPublisher
from .scanner import scan_source
from .store import StateStore

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "Config",
    "VideoPublisher",
    "VideoItem",
    "PublishResult",
    "PublishStatus",
    "StateStore",
    "scan_source",
    "PublisherError",
    "PublishError",
    "ConfigError",
    "ScanError",
    "ContentParseError",
    "BackendError",
    "AuthError",
    "UploadError",
    "RateLimitError",
    "ValidationError",
    "StateError",
]
