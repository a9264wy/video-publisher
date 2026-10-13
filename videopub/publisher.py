"""发布编排器。

职责：
  * 组织「取待发布队列 → 逐条校验 → 调用后端 → 重试 → 记录状态 → 汇总」；
  * 统一异常处理：区分可重试 / 不可重试，指数退避；
  * 产出批次报告（:class:`BatchReport`），供 CLI 与定时任务复用。

单条发布 :meth:`VideoPublisher.publish_one` 也被用作**模块对外的主接口**，
可被外部程序（如公众号流水线）直接调用。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from .backends import create_backend
from .config import Config
from .errors import BackendError, PublisherError, ValidationError
from .logger import get_logger, log_event
from .models import PublishResult, PublishStatus, VideoItem
from .scanner import pending_items, scan_source
from .store import StateStore
from .validators import validate_item

log = get_logger("publisher")


@dataclass
class BatchReport:
    batch_id: str
    backend: str
    trigger: str
    started_at: str = ""
    finished_at: str = ""
    results: list[PublishResult] = field(default_factory=list)
    source_dir: str = ""
    limit: int | None = None

    # -- 统计 --------------------------------------------------------------
    @property
    def backend_name(self) -> str:
        """别名，便于外部代码统一用 ``*_name`` 命名。"""
        return self.backend

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> list[PublishResult]:
        return [r for r in self.results if r.status.ok]

    @property
    def failed(self) -> list[PublishResult]:
        return [r for r in self.results if r.status == PublishStatus.FAILED]

    @property
    def skipped(self) -> list[PublishResult]:
        return [r for r in self.results if r.status == PublishStatus.SKIPPED]

    @property
    def duration_ms(self) -> int:
        try:
            a = datetime.fromisoformat(self.started_at)
            b = datetime.fromisoformat(self.finished_at)
            return int((b - a).total_seconds() * 1000)
        except (ValueError, TypeError):
            return sum(r.duration_ms for r in self.results)

    @property
    def ok(self) -> bool:
        return self.total > 0 and not self.failed

    def summary(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "backend": self.backend,
            "trigger": self.trigger,
            "source_dir": self.source_dir,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "total": self.total,
            "success": len(self.succeeded),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "ok": self.ok,
            "items": [r.to_dict() for r in self.results],
        }

    def to_dict(self) -> dict[str, Any]:
        return self.summary()


ProgressHook = Callable[[VideoItem, str, float], None]


class VideoPublisher:
    """对外主入口：构造后可反复调用 :meth:`run` / :meth:`publish_one`。"""

    def __init__(
        self,
        config: Config,
        *,
        backend: Any | None = None,
        store: StateStore | None = None,
        progress: ProgressHook | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.config = config
        config.ensure_dirs()
        self.store = store or StateStore(config.state_path)
        self.progress = progress
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self.backend_name = "dryrun" if self.dry_run else config.publish.backend
        if backend is not None:
            self.backend = backend
            self.backend_name = getattr(backend, "name", self.backend_name)
        else:
            self.backend = self._make_backend(self.backend_name)

    def _make_backend(self, name: str):
        if name == "dryrun":
            return create_backend("dryrun", self.config, progress_cb=self._emit)
        return create_backend(name, self.config)

    def _emit(self, item: VideoItem, phase: str, pct: float) -> None:
        if self.progress:
            try:
                self.progress(item, phase, pct)
            except Exception:
                pass

    def switch_backend(self, name: str, **kwargs: Any) -> None:
        self._close_backend()
        self.backend = create_backend(name, self.config, **kwargs)
        self.backend_name = name

    # ------------------------------------------------------------------ #
    # 单条发布
    # ------------------------------------------------------------------ #
    def publish_one(
        self,
        item: VideoItem,
        *,
        trigger: str = "manual",
        batch_id: str = "",
        skip_if_published: bool = True,
        raise_on_error: bool = False,
        options: dict[str, Any] | None = None,
    ) -> PublishResult:
        """发布一条内容。异常默认被捕获并转成 FAILED 结果（便于批量继续）。

        ``options`` 会原样透传给后端；目前用于传 ``scheduled_at``
        （例如 ``{"scheduled_at": "2026-10-14 08:00"}``）覆盖配置里的定时时间。
        """
        batch_id = batch_id or f"b-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        passthrough = dict(options or {})

        if skip_if_published and self.config.publish.skip_published and self.store.is_published(item.item_id):
            log.info("跳过（已发布）：%s", item.item_id)
            res = PublishResult(item_id=item.item_id, title=item.title, backend=self.backend_name,
                                trigger=trigger, batch_id=batch_id)
            res.detail["reason"] = "already_published"
            res.finish(PublishStatus.SKIPPED)
            self._log_result(res)
            return res

        log_event(log, 20, f"开始发布：{item.title}",
                  event="publish_start", item_id=item.item_id, title=item.title,
                  backend=self.backend_name, trigger=trigger, batch_id=batch_id)

        # 预校验：把可预知的失败挡在联网之前
        try:
            validate_item(item, self.config)
        except ValidationError as exc:
            res = PublishResult(item_id=item.item_id, title=item.title, backend=self.backend_name,
                                trigger=trigger, batch_id=batch_id, error_code=exc.code,
                                error_message=exc.message, attempts=0)
            res.detail.update(exc.detail)
            res.finish(PublishStatus.FAILED)
            self.store.record(res)
            self._log_result(res)
            if raise_on_error:
                raise
            return res

        max_attempts = max(int(self.config.publish.max_retries), 1)
        backoff = max(float(self.config.publish.retry_backoff_seconds), 0.0)
        last: PublishResult | None = None

        for attempt in range(1, max_attempts + 1):
            res = PublishResult(item_id=item.item_id, title=item.title, backend=self.backend_name,
                                trigger=trigger, batch_id=batch_id, attempts=attempt)
            t0 = time.perf_counter()
            try:
                if not self.backend.authenticated:
                    self.backend.authenticate()
                out = self.backend.publish(item, options={
                    "trigger": trigger, "batch_id": batch_id, "attempt": attempt,
                    **passthrough,
                })
                out.attempts = attempt
                out.trigger = trigger
                out.batch_id = batch_id
                out.duration_ms = out.duration_ms or int((time.perf_counter() - t0) * 1000)
                self.store.record(out)
                self._log_result(out)
                return out
            except PublisherError as exc:
                res.error_code = exc.code
                res.error_message = exc.message
                res.detail.update(exc.detail or {})
                res.duration_ms = int((time.perf_counter() - t0) * 1000)
                res.finish(PublishStatus.FAILED)
                last = res
                retryable = exc.retryable and attempt < max_attempts
                log.warning("发布失败[%s/%s] %s：%s%s", attempt, max_attempts, item.item_id, exc.message,
                            "，将重试" if retryable else "，不再重试")
                if retryable:
                    delay = backoff * (2 ** (attempt - 1))
                    log.info("退避 %.1fs 后重试", delay)
                    time.sleep(delay)
                    continue
                break
            except Exception as exc:  # 兜底：任何未预期异常都不应炸掉整批
                res.error_code = "EUNKNOWN"
                res.error_message = f"未预期异常: {exc!r}"
                res.duration_ms = int((time.perf_counter() - t0) * 1000)
                res.finish(PublishStatus.FAILED)
                last = res
                log.exception("未预期异常（%s）", item.item_id)
                break

        assert last is not None
        self.store.record(last)
        self._log_result(last)
        if raise_on_error:
            raise BackendError(last.error_message or "发布失败", detail=last.detail)
        return last

    # ------------------------------------------------------------------ #
    # 批量发布
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        limit: int | None = None,
        trigger: str = "manual",
        source_dir: str | None = None,
        items: Sequence[VideoItem] | None = None,
        skip_if_published: bool | None = None,
        options: dict[str, Any] | None = None,
    ) -> BatchReport:
        """扫描并发布一批内容。默认只取「待发布队列」的前 ``limit`` 条。

        ``options`` 透传给后端（如 ``scheduled_at`` 覆盖本次定时时间）。
        """
        batch_id = f"b-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        report = BatchReport(batch_id=batch_id, backend=self.backend_name, trigger=trigger,
                             source_dir=source_dir or self.config.source_dir, limit=limit,
                             started_at=datetime.now().isoformat(timespec="seconds"))
        batch_options = dict(options or {})

        if items is None:
            skip = self.config.publish.skip_published if skip_if_published is None else skip_if_published
            if skip:
                items = pending_items(self.config, self.store, source_dir=source_dir)
            else:
                items = scan_source(self.config, source_dir=source_dir)
        items = list(items)
        if limit is not None and limit > 0:
            items = items[:limit]

        log_event(log, 20, f"批次 {batch_id} 开始，共 {len(items)} 条",
                  event="batch_start", batch_id=batch_id, backend=self.backend_name,
                  trigger=trigger, total=len(items))

        if not items:
            report.finished_at = datetime.now().isoformat(timespec="seconds")
            log.warning("没有待发布内容（全部已发布或目录为空）")
            log_event(log, 30, "批次无可发布内容", event="batch_empty", batch_id=batch_id)
            return report

        for idx, item in enumerate(items, 1):
            log.info("—— [%d/%d] %s ——", idx, len(items), item.title)
            res = self.publish_one(item, trigger=trigger, batch_id=batch_id,
                                   skip_if_published=bool(skip_if_published if skip_if_published is not None
                                                          else self.config.publish.skip_published),
                                   options=batch_options)
            report.results.append(res)
            if res.status == PublishStatus.FAILED and self.config.publish.stop_on_error:
                log.error("已开启 stop_on_error，终止本批")
                break

        report.finished_at = datetime.now().isoformat(timespec="seconds")
        log_event(log, 20,
                  f"批次完成：成功 {len(report.succeeded)} / 失败 {len(report.failed)} / 跳过 {len(report.skipped)}",
                  event="batch_done", batch_id=batch_id, backend=self.backend_name, trigger=trigger,
                  total=report.total, success=len(report.succeeded), failed=len(report.failed),
                  skipped=len(report.skipped), duration_ms=report.duration_ms)
        return report

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _log_result(self, res: PublishResult) -> None:
        payload = {
            "event": "publish_result",
            "item_id": res.item_id,
            "title": res.title,
            "status": res.status.value,
            "backend": res.backend,
            "remote_id": res.remote_id,
            "error_code": res.error_code,
            "error_message": res.error_message,
            "attempts": res.attempts,
            "duration_ms": res.duration_ms,
            "trigger": res.trigger,
            "batch_id": res.batch_id,
        }
        level = 20 if res.status.ok else (30 if res.status == PublishStatus.SKIPPED else 40)
        log_event(log, level, f"{res.status.label}：{res.title}", **payload)

    def healthcheck(self) -> dict[str, Any]:
        info = {"backend": self.backend_name, "dry_run": self.dry_run}
        try:
            info["auth"] = self.backend.authenticate()
        except PublisherError as exc:
            info["auth_error"] = exc.to_dict()
        try:
            info["health"] = self.backend.healthcheck()
        except Exception as exc:
            info["health"] = {"ok": False, "error": repr(exc)}
        info["counts"] = self.store.counts()
        return info

    def _close_backend(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        self._close_backend()

    def __enter__(self) -> "VideoPublisher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
