"""配置加载与校验。

配置文件为 JSON。查找顺序（CLI 可用 ``--config`` 覆盖）：
  1. 显式传入的路径
  2. 环境变量 ``VIDEOPUB_CONFIG``
  3. 当前工作目录 ``config.json``
  4. 包内默认值
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import timing
from .errors import ConfigError

DEFAULT_SOURCE_DIR = r"H:\去1qa\少儿科普白版-20261013-20261013T083033Z-1-001\少儿科普白版-20261013"

_SENSITIVE_HINTS = ("secret", "token", "password", "passwd", "key", "credential")


def mask_secrets(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：任何层级中键名含敏感词的字符串值都替换为 ``***``。

    嵌套结构（如 ``credentials.openapi.secret``）同样生效——
    这是一处实际修过的缺陷：早期版本只脱敏了顶层键。
    """
    if _depth > 8:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                out[k] = mask_secrets(v, _depth + 1)
            elif isinstance(v, str) and any(h in str(k).lower() for h in _SENSITIVE_HINTS):
                out[k] = "***" if v else ""
            else:
                out[k] = v
        return out
    if isinstance(value, list):
        return [mask_secrets(v, _depth + 1) for v in value]
    return value


@dataclass
class ScheduleConfig:
    """定时发布配置。"""

    enabled: bool = True
    time: str = "08:00"            # 每日发布时刻 HH:MM
    daily_count: int = 3           # 每日发布条数
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # 0=周一
    timezone_offset: float | None = None   # 留空用本机时区
    max_runtime_minutes: int = 60  # 单次任务最长运行时间（防卡死）
    jitter_seconds: int = 0        # 随机抖动，避免整点并发

    def __post_init__(self) -> None:
        try:
            hh, mm = self.time.split(":")
            h, m = int(hh), int(mm)
        except (ValueError, AttributeError) as exc:
            raise ConfigError(
                f"schedule.time 格式应为 HH:MM，当前为 {self.time!r}", detail={"field": "schedule.time"}
            ) from exc
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ConfigError(f"schedule.time 越界: {self.time!r}", detail={"field": "schedule.time"})
        if self.daily_count < 1:
            raise ConfigError("schedule.daily_count 必须 >= 1", detail={"field": "schedule.daily_count"})


@dataclass
class PublishConfig:
    """发布行为配置。"""

    backend: str = "dryrun"                  # dryrun | openapi | channels_web
    publish_mode: str = "draft"              # draft(存草稿) | immediate(立即发布) | scheduled
    scheduled_at: str = ""                   # 绝对定时时间 "YYYY-MM-DD HH:MM"（优先级最高）
    scheduled_daily_time: str = ""           # 每日定时时刻 "HH:MM"，取「下一个该时刻」
    scheduled_delay_minutes: int = 0         # 上面两者都空时：现在 + N 分钟
    min_lead_minutes: int = 30               # 距现在至少留多少分钟（太近平台会立即发布）
    max_retries: int = 3
    retry_backoff_seconds: float = 5.0       # 指数退避基数
    request_timeout_seconds: int = 120
    upload_chunk_size_mb: int = 4
    stop_on_error: bool = False              # 单条失败是否中断整批
    skip_published: bool = True              # 跳过已成功发布的条目
    dedupe_by_fingerprint: bool = False      # 按内容指纹去重（跨目录改名场景）
    verify_after_upload: bool = True         # 上传后回查状态
    extra: dict[str, Any] = field(default_factory=dict)

    MODES = ("draft", "immediate", "scheduled")

    def __post_init__(self) -> None:
        if self.publish_mode not in self.MODES:
            raise ConfigError(
                f"publish.publish_mode 应为 {list(self.MODES)} 之一，当前为 {self.publish_mode!r}",
                detail={"field": "publish.publish_mode", "allowed": list(self.MODES)},
            )
        # 提前把格式校验掉，避免跑起来才发现写错
        if self.scheduled_at:
            timing.parse_datetime(self.scheduled_at)
        if self.scheduled_daily_time:
            timing.parse_hm(self.scheduled_daily_time)
        if self.publish_mode == "scheduled" and not (
            self.scheduled_at or self.scheduled_daily_time or self.scheduled_delay_minutes > 0
        ):
            raise ConfigError(
                "publish_mode=scheduled 时必须指定 scheduled_at / scheduled_daily_time "
                "之一（或设 scheduled_delay_minutes > 0）",
                detail={"field": "publish.publish_mode"},
            )
        # 平台「定时发表」与「保存草稿」互斥（页面明文提示），
        # 该互斥由 channels_web 在提交时体现（选定时后「保存草稿」不可用），此处不强制报错。

    def resolve_scheduled_at(self, *, now: dt.datetime | None = None,
                             override: str = "") -> dt.datetime | None:
        """算出本次要用的平台定时时间；``None`` 表示不定时（立即/草稿）。"""
        return timing.resolve_scheduled_at(
            publish_mode=self.publish_mode,
            absolute=(override or self.scheduled_at or ""),
            daily_time=self.scheduled_daily_time,
            delay_minutes=self.scheduled_delay_minutes,
            min_lead_minutes=self.min_lead_minutes,
            now=now,
        )


@dataclass
class ContentConfig:
    """内容解析与封面选择。"""

    cover_preference: str = "4x3"            # 视频号主推横版
    cover_patterns: list[str] = field(default_factory=lambda: [
        "封面-4x3.{ext}", "封面.{ext}", "cover.{ext}", "封面-3x4.{ext}",
    ])
    video_patterns: list[str] = field(default_factory=lambda: ["final.mp4", "*.mp4", "*.mov", "*.m4v"])
    copy_file: str = "发布.md"
    default_topics: list[str] = field(default_factory=list)
    append_topics: bool = True               # 将话题标签追加到描述尾部
    activity: str = ""                        # 发布时默认参加的活动名（网页端显示为「账号名 · 活动名」）；空 = 不参加
    activity_keyword: str = ""                # 「搜索活动」框里输入的关键词；空 = 用 activity 全名（实测短词更易命中）
    title_max_length: int = 30               # 视频号描述/标题长度约束
    description_max_length: int = 1000


@dataclass
class LimitsConfig:
    """平台侧硬性限制，用于预校验。"""

    max_video_size_mb: int = 2048            # 2GB
    min_video_size_kb: int = 10
    allowed_video_ext: list[str] = field(default_factory=lambda: [".mp4", ".mov", ".m4v", ".flv", ".wmv", ".avi"])
    max_cover_size_mb: int = 20
    allowed_cover_ext: list[str] = field(default_factory=lambda: [".png", ".jpg", ".jpeg", ".webp", ".bmp"])
    max_topics: int = 20


@dataclass
class Config:
    source_dir: str = DEFAULT_SOURCE_DIR
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    state_file: str = "state/publish_state.json"
    log_dir: str = "logs"
    report_dir: str = "reports"
    base_dir: str = ""
    credentials: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True

    # -- 构造 --------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, *, base_dir: str | None = None) -> "Config":
        base = Path(base_dir or os.getcwd()).resolve()
        candidate: Path | None = None
        for cand in (path, os.environ.get("VIDEOPUB_CONFIG"), base / "config.json"):
            if not cand:
                continue
            p = Path(cand)
            if not p.is_absolute():
                p = base / p
            if p.exists():
                candidate = p
                break
        raw: dict[str, Any] = {}
        if candidate:
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"配置文件 JSON 解析失败: {candidate}", detail={"error": str(exc), "path": str(candidate)}
                ) from exc
            base = candidate.parent
        cfg = cls.from_dict(raw, base_dir=str(base))
        cfg.base_dir = str(base)
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base_dir: str | None = None) -> "Config":
        raw = dict(raw or {})
        sections = {
            "schedule": ScheduleConfig,
            "publish": PublishConfig,
            "content": ContentConfig,
            "limits": LimitsConfig,
        }
        kwargs: dict[str, Any] = {}
        for key, klass in sections.items():
            sub = raw.pop(key, {}) or {}
            if not isinstance(sub, dict):
                raise ConfigError(f"配置节 {key} 必须是对象", detail={"field": key})
            known = {f.name for f in fields(klass)}
            unknown = set(sub) - known
            if unknown:
                raise ConfigError(
                    f"配置节 {key} 含未知字段: {sorted(unknown)}",
                    detail={"field": key, "unknown": sorted(unknown), "allowed": sorted(known)},
                )
            try:
                kwargs[key] = klass(**sub)
            except TypeError as exc:
                raise ConfigError(f"配置节 {key} 参数错误: {exc}", detail={"field": key}) from exc
        known_top = {f.name for f in fields(cls)}
        unknown_top = set(raw) - known_top
        if unknown_top:
            raise ConfigError(
                f"顶层配置含未知字段: {sorted(unknown_top)}",
                detail={"unknown": sorted(unknown_top), "allowed": sorted(known_top)},
            )
        kwargs.update(raw)
        cfg = cls(**kwargs)
        if base_dir:
            cfg.base_dir = base_dir
        return cfg

    # -- 派生路径 ----------------------------------------------------------
    @property
    def base_path(self) -> Path:
        return Path(self.base_dir or os.getcwd()).resolve()

    def resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.base_path / p)

    @property
    def state_path(self) -> Path:
        return self.resolve(self.state_file)

    @property
    def log_path(self) -> Path:
        return self.resolve(self.log_dir)

    @property
    def report_path(self) -> Path:
        return self.resolve(self.report_dir)

    @property
    def source_path(self) -> Path:
        return Path(self.source_dir)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": self.source_dir,
            "schedule": vars(self.schedule),
            "publish": {**vars(self.publish)},
            "content": vars(self.content),
            "limits": vars(self.limits),
            "state_file": self.state_file,
            "log_dir": self.log_dir,
            "report_dir": self.report_dir,
            "base_dir": self.base_dir,
            "credentials": mask_secrets(self.credentials),
            "dry_run": self.dry_run,
        }

    def ensure_dirs(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)
        self.report_path.mkdir(parents=True, exist_ok=True)
