"""定时发布调度器。

支持两种运行方式：
  * ``python main.py schedule``            —— 常驻守护进程，每日到点自动发布
  * ``python main.py schedule --once``     —— 只判断「现在是否该发」，适合交给
    系统计划任务（Windows 任务计划程序 / cron）每次唤醒调用一次

调度状态持久化在 ``state/schedule_state.json``，记录每个日期是否已执行，
避免进程重启或多次唤醒导致**同日重复发布**。
"""

from __future__ import annotations

import json
import os
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ScheduleError
from .logger import get_logger, log_event
from .publisher import BatchReport, VideoPublisher
from .store import StateStore

log = get_logger("scheduler")


@dataclass
class ScheduleState:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ScheduleState":
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"runs": {}}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except (json.JSONDecodeError, OSError):
                data = {"runs": {}}
        data.setdefault("runs", {})
        return cls(path=path, data=data)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("调度状态写入失败: %s", exc)

    def mark(self, day: str, **info: Any) -> None:
        self.data["runs"][day] = {"at": datetime.now().isoformat(timespec="seconds"), **info}
        self.data["last_run"] = self.data["runs"][day]
        self.save()

    def has_run(self, day: str) -> bool:
        return day in self.data["runs"]

    def last_run(self) -> dict[str, Any] | None:
        return self.data.get("last_run")


class Scheduler:
    def __init__(self, config: Config, publisher: VideoPublisher | None = None,
                 store: StateStore | None = None) -> None:
        self.config = config
        config.ensure_dirs()
        self.store = store or StateStore(config.state_path)
        self.publisher = publisher or VideoPublisher(config, store=self.store)
        self.state = ScheduleState.load(config.resolve("state/schedule_state.json"))
        self._stop = False

    # -- 时间计算 ----------------------------------------------------------
    @property
    def schedule(self):
        return self.config.schedule

    def next_run(self, now: datetime | None = None) -> datetime:
        """计算下一次应当执行的本地时间。"""
        now = now or datetime.now()
        hh, mm = (int(x) for x in self.schedule.time.split(":"))
        tgt = dtime(hour=hh, minute=mm)
        for offset in range(0, 15):  # 最多向后找 15 天
            day = (now + timedelta(days=offset)).date()
            if day.weekday() not in self.schedule.weekdays:
                continue
            candidate = datetime.combine(day, tgt)
            if candidate > now:
                return self._with_jitter(candidate)
        raise ScheduleError("未来 15 天内没有匹配的发布时刻，请检查 schedule.weekdays")

    def _with_jitter(self, moment: datetime) -> datetime:
        j = max(int(self.schedule.jitter_seconds), 0)
        return moment + timedelta(seconds=random.randint(0, j)) if j else moment

    def is_due(self, now: datetime | None = None) -> bool:
        """当前是否到达应发布时刻（且今日尚未执行）。"""
        now = now or datetime.now()
        if not self.schedule.enabled:
            return False
        if now.weekday() not in self.schedule.weekdays:
            return False
        hh, mm = (int(x) for x in self.schedule.time.split(":"))
        target = datetime.combine(now.date(), dtime(hour=hh, minute=mm))
        # 允许一个宽限窗口（默认 2 小时），避免休眠错过整点后当天不发布
        if not (target <= now <= target + timedelta(hours=2)):
            return False
        return not self.state.has_run(now.date().isoformat())

    # -- 执行 --------------------------------------------------------------
    def run_once(self, *, trigger: str = "schedule", limit: int | None = None,
                 force: bool = False) -> BatchReport:
        """执行一次每日发布任务。"""
        today = date.today().isoformat()
        if self.state.has_run(today) and not force:
            log.info("今日(%s)已执行过，跳过", today)
            report = BatchReport(batch_id="skipped", backend=self.publisher.backend_name, trigger=trigger)
            report.started_at = report.finished_at = datetime.now().isoformat(timespec="seconds")
            return report

        count = limit or self.schedule.daily_count
        # 平台原生定时（publish_mode=scheduled）时，本次运行的职责是**在平台侧挂上**
        # 一个定时任务，真正的发布由服务器执行 —— 因此本机不需要在发布时刻开机。
        target = self.config.publish.resolve_scheduled_at()
        if target is not None:
            log.info("本次将创建平台侧定时发布，时间 %s（服务器侧定时，不依赖本机开机）",
                     target.strftime("%Y-%m-%d %H:%M"))
        log_event(log, 20, f"定时任务启动：每日 {count} 条",
                  event="schedule_fire", trigger=trigger, daily_count=count,
                  source_dir=self.config.source_dir,
                  platform_scheduled_at=target.strftime("%Y-%m-%d %H:%M") if target else "")

        started = time.perf_counter()
        report = self.publisher.run(limit=count, trigger=trigger)
        elapsed = time.perf_counter() - started

        if elapsed > self.schedule.max_runtime_minutes * 60:
            log.warning("本次任务耗时 %.0fs，超过 max_runtime_minutes 配置", elapsed)

        self.state.mark(today, batch_id=report.batch_id, total=report.total,
                        success=len(report.succeeded), failed=len(report.failed),
                        trigger=trigger, duration_ms=int(elapsed * 1000))
        log_event(log, 20, f"定时任务结束：成功 {len(report.succeeded)} / 失败 {len(report.failed)}",
                  event="schedule_done", trigger=trigger, batch_id=report.batch_id,
                  success=len(report.succeeded), failed=len(report.failed))
        return report

    def daemon(self, *, poll_seconds: int = 30, limit: int | None = None) -> None:
        """常驻循环：睡到下一个时刻 → 执行 → 继续。Ctrl+C 优雅退出。"""
        self._install_signal_handlers()
        log.info("调度器已启动：每日 %s 自动发布 %d 条（源目录 %s）",
                 self.schedule.time, self.schedule.daily_count, self.config.source_dir)
        if self.schedule.jitter_seconds:
            log.info("已启用随机抖动 %ds", self.schedule.jitter_seconds)

        while not self._stop:
            try:
                nxt = self.next_run()
            except ScheduleError as exc:
                log.error("%s", exc.message)
                return
            wait = (nxt - datetime.now()).total_seconds()
            log.info("下一次发布：%s（还有 %.1f 小时）", nxt.strftime("%Y-%m-%d %H:%M:%S"), wait / 3600)

            # 分段睡眠，保证可及时响应停止信号
            while wait > 0 and not self._stop:
                nap = min(poll_seconds, wait)
                time.sleep(max(nap, 0.5))
                wait = (nxt - datetime.now()).total_seconds()

            if self._stop:
                break
            try:
                self.run_once(trigger="schedule", limit=limit)
            except Exception as exc:  # 定时任务必须自愈，不能因单次失败退出
                log.exception("定时发布执行失败：%r", exc)
                log_event(log, 40, f"定时发布异常：{exc}", event="schedule_error", error=repr(exc))
            # 防止同一天重复触发
            time.sleep(poll_seconds)

        log.info("调度器已停止")
        self.publisher.close()

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            log.info("收到停止信号(%s)，将在当前任务结束后退出…", signum)
            self._stop = True

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # 非主线程等情况忽略

    # -- 查询 --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        try:
            nxt = self.next_run().isoformat(timespec="seconds") if self.schedule.enabled else None
        except ScheduleError as exc:
            nxt = f"<错误: {exc.message}>"
        target = self.config.publish.resolve_scheduled_at()
        return {
            "enabled": self.schedule.enabled,
            "time": self.schedule.time,
            "daily_count": self.schedule.daily_count,
            "weekdays": self.schedule.weekdays,
            "next_run": nxt,
            "last_run": self.state.last_run(),
            "source_dir": self.config.source_dir,
            "backend": self.publisher.backend_name,
            "publish_mode": self.config.publish.publish_mode,
            # publish_mode=scheduled 时：本机到点只负责「挂定时」，
            # 真正对外发布由平台服务器在这个时刻执行
            "platform_target": (target.strftime("%Y-%m-%d %H:%M") if target else None),
        }
