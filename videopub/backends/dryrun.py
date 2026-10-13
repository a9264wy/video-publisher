"""演练后端（默认）。

不联网、不产生任何外部副作用，但**完整走一遍**真实流程的关键环节：
预校验 → 计算分片 → 模拟上传（带进度） → 模拟远端处理轮询 → 生成结果。

用途：
  * 首次接入时验证目录结构、文案解析、封面选择、状态机、日志与报表；
  * 定时任务的「彩排」，确保每天 3 条的调度逻辑正确；
  * 在拿到真实凭证前，把整条流水线跑通。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..config import Config
from ..errors import UploadError, ValidationError
from ..logger import get_logger
from ..models import PublishResult, PublishStatus, VideoItem
from ..validators import validate_item
from .base import PublishBackend

log = get_logger("backend.dryrun")


class DryRunBackend(PublishBackend):
    name = "dryrun"

    def __init__(self, config: Config, *, simulate_failures: bool = False,
                 progress_cb=None) -> None:
        super().__init__(config)
        self.simulate_failures = simulate_failures
        self.progress_cb = progress_cb

    def authenticate(self) -> dict[str, Any]:
        self._auth_info = {"mode": "dry-run", "account": "演练账号（不产生真实发布）"}
        self._authenticated = True
        return self._auth_info

    def _report(self, item: VideoItem, phase: str, pct: float) -> None:
        log.info("[演练] %s | %-14s %5.1f%%", item.item_id, phase, pct)
        if self.progress_cb:
            try:
                self.progress_cb(item, phase, pct)
            except Exception:  # 进度回调不应影响主流程
                pass

    def publish(self, item: VideoItem, *, options: dict[str, Any] | None = None) -> PublishResult:
        opts = options or {}
        result = self._result(item, options={**opts, "backend_name": self.name})
        t0 = time.perf_counter()

        # 1) 预校验（真实后端同样会做，这里提前暴露问题）
        info = validate_item(item, self.config)
        result.detail["validation"] = info

        # 2) 分片规划
        chunk_mb = max(self.config.publish.upload_chunk_size_mb, 1)
        total_mb = item.video_size / 1024 / 1024
        chunks = max(int(total_mb // chunk_mb) + 1, 1)
        result.detail["plan"] = {
            "chunk_size_mb": chunk_mb,
            "chunks": chunks,
            "video_size_mb": round(total_mb, 2),
            "cover": item.cover_path,
            "topics": item.hashtags(),
        }

        # 3) 模拟上传（快速推进进度，打印同真实流程一致的阶段）
        for i, phase in enumerate(["校验通过", "初始化上传", "分片上传", "合并文件", "上传封面"]):
            self._report(item, phase, (i + 1) / 5 * 90)
        time.sleep(0.02)

        # 4) 模拟远端生成 / 审核状态
        fingerprint = hashlib.md5(item.fingerprint.encode()).hexdigest()
        result.remote_id = f"dryrun-{fingerprint[:12]}"
        result.remote_url = f"https://channels.weixin.qq.com/dryrun/{result.remote_id}"
        result.attempts = opts.get("attempt", 1)

        if self.simulate_failures and int(fingerprint[:2], 16) % 5 == 0:
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
            result.error_code = "EUPLOAD"
            result.error_message = "演练注入的模拟上传失败（用于验证重试与错误处理）"
            result.finish(PublishStatus.FAILED)
            raise UploadError(result.error_message, detail={"simulated": True})

        result.detail["simulated_phases"] = ["upload", "cover", "publish", "audit"]
        result.finish(PublishStatus.DRY_RUN)
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        self._report(item, "演练完成", 100.0)
        return result
