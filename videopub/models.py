"""领域模型：待发布条目与发布结果。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class PublishStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

    @property
    def label(self) -> str:
        return {
            "pending": "待发布",
            "running": "发布中",
            "success": "成功",
            "failed": "失败",
            "skipped": "已跳过",
            "dry_run": "演练通过",
        }[self.value]

    @property
    def ok(self) -> bool:
        return self in (PublishStatus.SUCCESS, PublishStatus.DRY_RUN)


@dataclass
class VideoItem:
    """一条待发布的视频内容（对应源目录中的一个子目录）。"""

    item_id: str
    directory: str
    title: str
    description: str = ""
    video_path: str = ""
    cover_path: str = ""
    cover_3x4: str = ""
    cover_4x3: str = ""
    topics: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- 便捷属性 ----------------------------------------------------------
    @property
    def video_size(self) -> int:
        try:
            return Path(self.video_path).stat().st_size
        except OSError:
            return 0

    @property
    def fingerprint(self) -> str:
        """内容指纹：用于识别「同一内容重复发布」。"""
        raw = f"{self.item_id}|{self.title}|{self.video_size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def hashtags(self) -> list[str]:
        return [t if t.startswith("#") else f"#{t}" for t in self.topics]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["video_size"] = self.video_size
        d["fingerprint"] = self.fingerprint
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VideoItem":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class PublishResult:
    """一次发布尝试的完整结果。"""

    item_id: str
    title: str
    status: PublishStatus = PublishStatus.PENDING
    backend: str = "dryrun"
    remote_id: str = ""
    remote_url: str = ""
    error_code: str = ""
    error_message: str = ""
    attempts: int = 0
    duration_ms: int = 0
    started_at: str = field(default_factory=_now_iso)
    finished_at: str = ""
    trigger: str = "manual"          # manual | schedule | cli
    batch_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def finish(self, status: PublishStatus) -> "PublishResult":
        self.status = status
        self.finished_at = _now_iso()
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["status_label"] = self.status.label
        d["ok"] = self.status.ok
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PublishResult":
        d = dict(d)
        d.pop("status_label", None)
        d.pop("ok", None)
        st = d.get("status", "pending")
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        obj = cls(**{k: v for k, v in d.items() if k in allowed})
        obj.status = PublishStatus(st)
        return obj

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)
