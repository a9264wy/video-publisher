#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""异常处理与核心流程自测（纯标准库 unittest，无第三方依赖）。

运行：
    python tests/test_error_handling.py
或：
    python -m unittest discover -s tests -v

覆盖：
  * 配置校验（非法字段 / 非法时间 / 非法条数）
  * 源目录缺失、目录内无内容
  * 单条校验失败（视频缺失 / 格式不支持 / 描述超长 / 话题超限）
  * 可重试错误的重试与退避、不可重试错误的立即失败
  * 幂等：已成功条目跳过；--force 语义
  * 状态存储：记录、历史、统计、损坏状态文件自愈
  * 定时器的下次执行时间计算与「同日不重复」
  * 后端依赖缺失时的友好报错（channels_web / playwright）
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from videopub.backends import create_backend, list_backends          # noqa: E402
from videopub.config import Config                                    # noqa: E402
from videopub.errors import (                                         # noqa: E402
    AuthError,
    BackendError,
    ConfigError,
    DependencyError,
    PublisherError,
    RateLimitError,
    RemoteRejectError,
    ScanError,
    UploadError,
    ValidationError,
)
from videopub.models import PublishResult, PublishStatus, VideoItem   # noqa: E402
from videopub.publisher import VideoPublisher                         # noqa: E402
from videopub.scanner import scan_source                              # noqa: E402
from videopub.store import StateStore                                 # noqa: E402
from videopub import timing                                           # noqa: E402
from videopub.validators import validate_item                          # noqa: E402


def _mini_config(base: Path, **overrides) -> Config:
    cfg = Config.load(None, base_dir=str(base))
    cfg.base_dir = str(base)
    cfg.state_file = "state/publish_state.json"
    cfg.log_dir = "logs"
    cfg.report_dir = "reports"
    cfg.dry_run = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


def _make_source(root: Path, name: str, *, title: str | None = None,
                 video: bytes = b"0" * (20 * 1024),
                 with_copy: bool = True, with_cover: bool = True) -> Path:
    """构造一个符合源目录约定的条目目录。"""
    import re
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "final.mp4").write_bytes(video)
    if with_copy:
        t = title or re.sub(r"^\s*\d+\s*[-_.、]\s*", "", name)
        (d / "发布.md").write_text(
            f"# 《{t}》· 发布文案\n\n## 视频号（横版 4:3 封面）\n```\n《{t}》\n副标题\n```\n"
            "简介：\n```\n这是简介正文。\n#少儿科普 #白版\n```\n",
            encoding="utf-8",
        )
    if with_cover:
        # 合法的最小 PNG（1x1 透明）——用于测试存在性 / 格式 / 尺寸解析分支
        (d / "封面-4x3.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        ))
    return d


class ConfigTests(unittest.TestCase):
    def test_unknown_section_field_rejected(self):
        with self.assertRaises(ConfigError) as cm:
            Config.from_dict({"schedule": {"timee": "08:00"}})
        self.assertEqual(cm.exception.code, "ECONFIG")

    def test_unknown_top_field_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"nope": 1})

    def test_bad_time_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"schedule": {"time": "8点"}})

    def test_out_of_range_time_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"schedule": {"time": "25:00"}})

    def test_bad_daily_count_rejected(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"schedule": {"daily_count": 0}})

    def test_secrets_masked(self):
        cfg = Config.from_dict({"credentials": {"openapi": {"secret": "abc", "appid": "wx1"}}})
        self.assertEqual(cfg.to_dict()["credentials"]["openapi"]["secret"], "***")
        self.assertEqual(cfg.to_dict()["credentials"]["openapi"]["appid"], "wx1")

    def test_missing_json_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config.load(str(Path(td) / "not-exist.json"), base_dir=td)
            self.assertEqual(cfg.schedule.daily_count, 3)


class ScannerTests(unittest.TestCase):
    def test_missing_source_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.source_dir = str(Path(td) / "nope")
            with self.assertRaises(ScanError) as cm:
                scan_source(cfg)
            self.assertEqual(cm.exception.code, "ESCAN")

    def test_empty_source_dir(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            src = Path(td) / "src"
            src.mkdir()
            cfg.source_dir = str(src)
            with self.assertRaises(ScanError):
                scan_source(cfg)

    def test_scan_parses_copy_and_cover(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            _make_source(src, "01-风是从哪来的")
            _make_source(src, "02-海水为什么是咸的")
            cfg.source_dir = str(src)
            items = scan_source(cfg)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].title, "风是从哪来的")
            self.assertEqual(items[0].cover_path, str(src / "01-风是从哪来的" / "封面-4x3.png"))
            self.assertIn("少儿科普", items[0].topics)
            self.assertTrue(items[0].description)
            # 描述里的话题不应把 md 标题切乱（回归用例）
            self.assertIn("这是简介正文", items[0].description)

    def test_scan_sorted_numerically(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            for n in ("10-甲", "2-乙", "1-丙"):
                _make_source(src, n)
            cfg.source_dir = str(src)
            ids = [i.item_id for i in scan_source(cfg)]
            self.assertEqual(ids, ["1-丙", "2-乙", "10-甲"])

    def test_scan_warns_on_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            _make_source(src, "01-缺料", with_copy=False, with_cover=False)
            cfg.source_dir = str(src)
            item = scan_source(cfg)[0]
            warns = " ".join(item.meta.get("warnings", []))
            self.assertIn("封面", warns)


class ValidatorTests(unittest.TestCase):
    def _item(self, base: Path, **kw) -> VideoItem:
        i = VideoItem(item_id="t", directory=str(base), title="标题")
        i.video_path = str(base / "v.mp4")
        i.cover_path = str(base / "c.png")
        Path(i.video_path).write_bytes(b"0" * (50 * 1024))
        Path(i.cover_path).write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        ))
        for k, v in kw.items():
            setattr(i, k, v)
        return i

    def test_missing_video(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            it = self._item(Path(td))
            Path(it.video_path).unlink()
            with self.assertRaises(ValidationError) as cm:
                validate_item(it, cfg)
            self.assertIn("视频文件缺失", str(cm.exception.detail["errors"]))

    def test_bad_video_ext(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            it = self._item(Path(td))
            it.video_path = str(Path(td) / "v.txt")
            Path(it.video_path).write_bytes(b"0" * (50 * 1024))
            with self.assertRaises(ValidationError):
                validate_item(it, cfg)

    def test_video_too_small(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            it = self._item(Path(td))
            Path(it.video_path).write_bytes(b"0" * 1024)
            with self.assertRaises(ValidationError):
                validate_item(it, cfg)

    def test_empty_title(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            it = self._item(Path(td), title="   ")
            with self.assertRaises(ValidationError):
                validate_item(it, cfg)

    def test_too_many_topics(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.limits.max_topics = 2
            it = self._item(Path(td), topics=["a", "b", "c"])
            with self.assertRaises(ValidationError):
                validate_item(it, cfg)

    def test_ok(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            it = self._item(Path(td))
            info = validate_item(it, cfg)
            self.assertEqual(info["item_id"], "t")
            self.assertIn("warnings", info)


class FakeBackend:
    """可编程后端：按脚本依次抛异常 / 返回结果。"""

    name = "fake"

    def __init__(self, script, config=None):
        self.script = list(script)
        self.calls = 0
        self.authenticated = False

    def authenticate(self):
        self.authenticated = True
        return {"mode": "fake"}

    def publish(self, item, *, options=None):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        r = PublishResult(item_id=item.item_id, title=item.title, backend=self.name)
        r.remote_id = "fake-1"
        r.finish(PublishStatus.SUCCESS)
        return r

    def healthcheck(self):
        return {"backend": self.name, "ok": True}


class RetryTests(unittest.TestCase):
    def _pub(self, base: Path, script) -> tuple[VideoPublisher, FakeBackend]:
        cfg = _mini_config(base)
        cfg.publish.retry_backoff_seconds = 0  # 测试里不退避等待
        cfg.publish.max_retries = 3
        backend = FakeBackend(script, cfg)
        pub = VideoPublisher(cfg, backend=backend, store=StateStore(cfg.state_path))
        return pub, backend

    def _item(self, base: Path) -> VideoItem:
        s = base / "01-x"
        _make_source(s.parent, "01-x")
        return VideoItem(item_id="01-x", directory=str(s), title="x",
                         video_path=str(s / "final.mp4"), cover_path=str(s / "封面-4x3.png"))

    def test_retry_then_success(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, backend = self._pub(base, [UploadError("超时"), RateLimitError("限流"), None])
            res = pub.publish_one(self._item(base))
            self.assertEqual(res.status, PublishStatus.SUCCESS)
            self.assertEqual(backend.calls, 3)
            self.assertEqual(res.attempts, 3)

    def test_exhaust_retries(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, backend = self._pub(base, [UploadError("e1"), UploadError("e2"), UploadError("e3")])
            res = pub.publish_one(self._item(base))
            self.assertEqual(res.status, PublishStatus.FAILED)
            self.assertEqual(res.error_code, "EUPLOAD")
            self.assertEqual(backend.calls, 3)

    def test_non_retryable_fails_fast(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, backend = self._pub(base, [AuthError("登录态失效"), None])
            res = pub.publish_one(self._item(base))
            self.assertEqual(res.status, PublishStatus.FAILED)
            self.assertEqual(res.error_code, "EAUTH")
            self.assertEqual(backend.calls, 1, "鉴权错误不应重试")

    def test_remote_reject_no_retry(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, backend = self._pub(base, [RemoteRejectError("审核不通过"), None])
            res = pub.publish_one(self._item(base))
            self.assertEqual(res.error_code, "EREJECT")
            self.assertEqual(backend.calls, 1)

    def test_unexpected_exception_contained(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, _ = self._pub(base, [RuntimeError("boom")])
            res = pub.publish_one(self._item(base))
            self.assertEqual(res.status, PublishStatus.FAILED)
            self.assertEqual(res.error_code, "EUNKNOWN")

    def test_validation_failure_never_reaches_backend(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, backend = self._pub(base, [None])
            it = self._item(base)
            Path(it.video_path).unlink()
            res = pub.publish_one(it)
            self.assertEqual(res.status, PublishStatus.FAILED)
            self.assertEqual(res.error_code, "EVALID")
            self.assertEqual(backend.calls, 0, "校验失败不应调用后端")

    def test_raise_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pub, _ = self._pub(base, [AuthError("x")])
            with self.assertRaises(PublisherError):
                pub.publish_one(self._item(base), raise_on_error=True)


class IdempotencyTests(unittest.TestCase):
    def _item(self, base: Path) -> VideoItem:
        _make_source(base, "01-x")
        s = base / "01-x"
        return VideoItem(item_id="01-x", directory=str(s), title="x",
                         video_path=str(s / "final.mp4"), cover_path=str(s / "封面-4x3.png"))

    def test_skip_published(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            cfg.publish.retry_backoff_seconds = 0
            store = StateStore(cfg.state_path)
            backend = FakeBackend([None, None])
            pub = VideoPublisher(cfg, backend=backend, store=store)
            it = self._item(base)
            first = pub.publish_one(it)
            self.assertEqual(first.status, PublishStatus.SUCCESS)
            second = pub.publish_one(it)
            self.assertEqual(second.status, PublishStatus.SKIPPED)
            self.assertEqual(backend.calls, 1, "已成功条目不应重复发布")

    def test_force_resend(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            cfg.publish.retry_backoff_seconds = 0
            store = StateStore(cfg.state_path)
            backend = FakeBackend([None, None])
            pub = VideoPublisher(cfg, backend=backend, store=store)
            it = self._item(base)
            pub.publish_one(it)
            pub.publish_one(it, skip_if_published=False)
            self.assertEqual(backend.calls, 2)

    def test_batch_limit_and_pending(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            cfg.publish.retry_backoff_seconds = 0
            src = base / "src"
            for n in ("01-a", "02-b", "03-c", "04-d"):
                _make_source(src, n)
            cfg.source_dir = str(src)
            backend = FakeBackend([None] * 10)
            pub = VideoPublisher(cfg, backend=backend, store=StateStore(cfg.state_path))
            report = pub.run(limit=3)
            self.assertEqual(report.total, 3)
            self.assertEqual(len(report.succeeded), 3)
            # 再来一批：应只剩 1 条待发布
            report2 = pub.run(limit=3)
            self.assertEqual(report2.total, 1)

    def test_stop_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            cfg.publish.retry_backoff_seconds = 0
            cfg.publish.max_retries = 1
            cfg.publish.stop_on_error = True
            src = base / "src"
            for n in ("01-a", "02-b", "03-c"):
                _make_source(src, n)
            cfg.source_dir = str(src)
            backend = FakeBackend([AuthError("bad"), None, None])
            pub = VideoPublisher(cfg, backend=backend, store=StateStore(cfg.state_path))
            report = pub.run(limit=3)
            self.assertEqual(report.total, 1)
            self.assertFalse(report.ok)


class StoreTests(unittest.TestCase):
    def test_record_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.json")
            for i, st in enumerate([PublishStatus.SUCCESS, PublishStatus.FAILED, PublishStatus.SKIPPED]):
                r = PublishResult(item_id=f"i{i}", title=f"t{i}")
                r.finish(st)
                store.record(r)
            c = store.counts()
            self.assertEqual(c["success"], 1)
            self.assertEqual(c["failed"], 1)
            self.assertEqual(c["total"], 3)
            self.assertEqual(len(store.history()), 3)
            self.assertTrue(store.is_published("i0"))
            self.assertFalse(store.is_published("i1"))

    def test_dry_run_not_counted_as_published(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.json")
            r = PublishResult(item_id="i", title="t")
            r.finish(PublishStatus.DRY_RUN)
            store.record(r)
            self.assertFalse(store.is_published("i"))
            self.assertTrue(store.is_published("i", include_dry_run=True))

    def test_corrupt_state_self_heals(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            p.write_text("{not json", encoding="utf-8")
            store = StateStore(p)
            self.assertEqual(store.counts()["total"], 0)
            backups = list(Path(td).glob("s.corrupt-*.json"))
            self.assertTrue(backups, "损坏的状态文件应被备份")

    def test_history_filters(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.json")
            for st in (PublishStatus.SUCCESS, PublishStatus.FAILED):
                r = PublishResult(item_id="i", title="t", backend="dryrun")
                r.finish(st)
                store.record(r)
            self.assertEqual(len(store.history(status="success")), 1)
            self.assertEqual(len(store.history(backend="dryrun")), 2)
            self.assertEqual(len(store.history(item_id="i")), 2)
            self.assertEqual(len(store.history(limit=1)), 1)

    def test_reset(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / "s.json")
            r = PublishResult(item_id="i", title="t")
            r.finish(PublishStatus.SUCCESS)
            store.record(r)
            self.assertTrue(store.reset("i"))
            self.assertFalse(store.is_published("i"))
            self.assertFalse(store.reset("i"))


class SchedulerTests(unittest.TestCase):
    def test_next_run_rolls_to_tomorrow(self):
        from videopub.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.schedule.time = "08:00"
            cfg.schedule.jitter_seconds = 0
            s = Scheduler(cfg)
            now = datetime(2026, 10, 13, 9, 0)
            nxt = s.next_run(now)
            self.assertEqual(nxt.date(), (now + timedelta(days=1)).date())
            self.assertEqual((nxt.hour, nxt.minute), (8, 0))

    def test_next_run_today_when_before(self):
        from videopub.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.schedule.time = "08:00"
            cfg.schedule.jitter_seconds = 0
            s = Scheduler(cfg)
            now = datetime(2026, 10, 13, 6, 30)
            nxt = s.next_run(now)
            self.assertEqual(nxt.date(), now.date())

    def test_weekday_filter(self):
        from videopub.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.schedule.time = "08:00"
            cfg.schedule.jitter_seconds = 0
            cfg.schedule.weekdays = [0]  # 仅周一
            s = Scheduler(cfg)
            # 2026-10-13 是周二 → 下一次应落到下周一 2026-10-19
            nxt = s.next_run(datetime(2026, 10, 13, 9, 0))
            self.assertEqual(nxt.weekday(), 0)
            self.assertEqual(nxt.date().isoformat(), "2026-10-19")

    def test_no_duplicate_same_day(self):
        from videopub.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            for n in ("01-a", "02-b", "03-c", "04-d"):
                _make_source(src, n)
            cfg.source_dir = str(src)
            cfg.schedule.time = "08:00"
            cfg.schedule.daily_count = 3
            cfg.schedule.jitter_seconds = 0
            s = Scheduler(cfg)
            r1 = s.run_once(trigger="test", force=True)
            self.assertEqual(len(r1.succeeded), 3)
            r2 = s.run_once(trigger="test")  # 同日第二次，应被抑制
            self.assertEqual(r2.total, 0)
            self.assertEqual(r2.batch_id, "skipped")

    def test_disabled_schedule_not_due(self):
        from videopub.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.schedule.enabled = False
            s = Scheduler(cfg)
            self.assertFalse(s.is_due(datetime(2026, 10, 13, 8, 30)))


class BackendTests(unittest.TestCase):
    def test_unknown_backend(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            with self.assertRaises(BackendError) as cm:
                create_backend("nope", cfg)
            self.assertEqual(cm.exception.code, "EBACKEND")
            self.assertIn("dryrun", cm.exception.detail["available"])

    def test_openapi_requires_base_url(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            cfg.credentials = {"openapi": {}}
            with self.assertRaises(BackendError):
                create_backend("openapi", cfg)

    def test_channel_web_missing_dependency_is_friendly(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _mini_config(Path(td))
            b = create_backend("channels_web", cfg)
            health = b.healthcheck()
            has_pw = any(c["check"] == "dependency:playwright" and c["ok"] for c in health["checks"])
            if not has_pw:
                self.assertFalse(health["ok"])
                self.assertIn("playwright", health["error"])

    def test_backend_listing(self):
        names = {b["name"] for b in list_backends()}
        self.assertEqual(names, {"dryrun", "openapi", "channels_web"})

    def test_channels_web_selectors_consistent(self):
        """每个选择器键都要有非空候选列表，且代码里用到的键必须存在。

        这是防止「改了代码忘了改选择器」的低成本回归网。
        """
        from videopub.backends import channels_web as cw

        required = {
            "file_input", "desc_editor", "short_title", "cover_input",
            "publish_btn", "draft_btn", "login_qr", "uploading_hint",
        }
        for key in required:
            self.assertIn(key, cw.SELECTORS, f"缺少选择器键: {key}")
        for key, sels in cw.SELECTORS.items():
            self.assertIsInstance(sels, list, f"{key} 应为列表")
            self.assertTrue(sels, f"{key} 候选为空")
            for s in sels:
                self.assertIsInstance(s, str)
                self.assertTrue(s.strip(), f"{key} 含空选择器")

    def test_channel_web_uses_locator_not_query_selector(self):
        """必须用 playwright locator（能穿透 Shadow DOM），不能用 query_selector。

        视频号助手的表单在 Shadow DOM 内，原生 querySelector 采不到控件 ——
        这是实测踩过的坑，加断言防止回退。
        """
        src = (ROOT / "videopub" / "backends" / "channels_web.py").read_text(encoding="utf-8")
        self.assertNotIn("query_selector(", src)
        self.assertIn("page.locator(", src)

    def test_dryrun_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            _make_source(src, "01-a")
            cfg.source_dir = str(src)
            pub = VideoPublisher(cfg, dry_run=True)
            res = pub.publish_one(scan_source(cfg)[0])
            self.assertEqual(res.status, PublishStatus.DRY_RUN)
            self.assertTrue(res.remote_id.startswith("dryrun-"))
            self.assertIn("plan", res.detail)


class CliTests(unittest.TestCase):
    """CLI 层：参数位置兼容性与错误码透出。"""

    def _run(self, argv):
        import contextlib
        import io
        from videopub.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_global_option_before_and_after_subcommand(self):
        from videopub.cli import build_parser
        p = build_parser()
        # 子命令之后
        a = p.parse_args(["scan", "--source-dir", r"H:\x", "--backend", "dryrun"])
        self.assertEqual(a.source_dir, r"H:\x")
        self.assertEqual(a.backend, "dryrun")
        # 子命令之前
        b = p.parse_args(["--source-dir", r"H:\y", "scan"])
        self.assertEqual(b.source_dir, r"H:\y")
        # 未提供时保持顶层默认，不应因 SUPPRESS 而丢失属性
        c = p.parse_args(["scan"])
        self.assertIsNone(c.source_dir)
        self.assertFalse(c.live)

    def test_scan_missing_dir_returns_error_code(self):
        with tempfile.TemporaryDirectory() as td:
            code, out = self._run(["--config", str(Path(td) / "nope.json"),
                                   "scan", "--source-dir", str(Path(td) / "missing")])
            self.assertEqual(code, 1)
            self.assertIn("ESCAN", out)

    def test_bad_config_surfaces_econfig(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "bad.json"
            cfg.write_text('{"schedule": {"timee": "08:00"}}', encoding="utf-8")
            code, out = self._run(["--config", str(cfg), "scan"])
            self.assertEqual(code, 1)
            self.assertIn("ECONFIG", out)

    def test_openapi_without_base_url_surfaces_ebackend(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "c.json"
            cfg.write_text('{"credentials": {"openapi": {}}}', encoding="utf-8")
            src = Path(td) / "src"
            _make_source(src, "01-a")
            code, out = self._run(["--config", str(cfg), "publish",
                                   "--source-dir", str(src), "--live", "--backend", "openapi"])
            self.assertEqual(code, 1)
            self.assertIn("EBACKEND", out)

    def test_backends_listing(self):
        code, out = self._run(["backends"])
        self.assertEqual(code, 0)
        for n in ("dryrun", "openapi", "channels_web"):
            self.assertIn(n, out)

    def test_logging_handles_released_after_run(self):
        """CLI 退出后必须释放日志文件句柄，否则 Windows 下日志目录无法删除。"""
        import shutil as _sh
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = base / "c.json"
            src = base / "src"
            _make_source(src, "01-a")
            cfg.write_text(json.dumps({"source_dir": str(src), "log_dir": "logs",
                                       "state_file": "state/s.json", "report_dir": "reports"}),
                           encoding="utf-8")
            code, _ = self._run(["--config", str(cfg), "status", "--no-pending"])
            self.assertEqual(code, 0)
            self.assertTrue((base / "logs").exists(), "应已生成日志目录")
            # 句柄已释放 → 目录可删除
            _sh.rmtree(base / "logs")
            self.assertFalse((base / "logs").exists())


class InitTests(unittest.TestCase):
    def test_package_exports(self):
        import videopub

        for name in ("Config", "VideoPublisher", "VideoItem", "PublishResult", "PublishStatus",
                     "StateStore", "scan_source"):
            self.assertTrue(hasattr(videopub, name), f"缺少导出: {name}")

    def test_public_single_call_api(self):
        """模拟外部程序直接调用模块的主接口。"""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = _mini_config(base)
            src = base / "src"
            _make_source(src, "01-a")
            cfg.source_dir = str(src)
            with VideoPublisher(cfg) as pub:
                report = pub.run(limit=1, trigger="api")
            self.assertEqual(len(report.succeeded), 1)
            payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
            self.assertEqual(payload["success"], 1)


class TimingTests(unittest.TestCase):
    """平台定时时间解析（videopub.timing）。

    这块逻辑直接关乎**误发风险**：本该「明天 8 点」发的内容，一旦时间没算对
    或没设上，点「发表」就当场发出去了，而且不可撤销。所以边界必须钉死。
    """

    def test_not_scheduled_returns_none(self):
        self.assertIsNone(timing.resolve_scheduled_at(publish_mode="draft",
                                                      daily_time="08:00"))
        self.assertIsNone(timing.resolve_scheduled_at(publish_mode="immediate",
                                                      absolute="2030-01-01 08:00"))
        # 即使显式给了时刻，非 scheduled 模式也不该产生定时目标
        self.assertIsNone(timing.resolve_scheduled_at(publish_mode="draft",
                                                      delay_minutes=60))

    def test_absolute_wins_and_parses(self):
        now = datetime(2026, 10, 13, 17, 50)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          absolute="2026-10-14 08:00", now=now)
        self.assertEqual(got, datetime(2026, 10, 14, 8, 0))

    def test_daily_time_rolls_to_tomorrow_when_already_passed(self):
        now = datetime(2026, 10, 13, 17, 50)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          daily_time="08:00", now=now)
        self.assertEqual(got, datetime(2026, 10, 14, 8, 0))

    def test_daily_time_keeps_today_when_still_ahead(self):
        now = datetime(2026, 10, 13, 5, 0)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          daily_time="08:00", now=now)
        self.assertEqual(got, datetime(2026, 10, 13, 8, 0))

    def test_min_lead_floor(self):
        """时间窗太近时抬高到安全水位（否则平台按「立即发布」处理）。"""
        now = datetime(2026, 10, 13, 7, 55)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          daily_time="08:00", now=now,
                                          min_lead_minutes=30)
        self.assertEqual(got, datetime(2026, 10, 13, 8, 25))

    def test_delay_minutes(self):
        now = datetime(2026, 10, 13, 10, 0)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          delay_minutes=120, now=now)
        self.assertEqual(got, datetime(2026, 10, 13, 12, 0))

    def test_seconds_are_truncated(self):
        """picker 只支持到分钟，秒必须被抹掉，否则回读校验必然不相等。"""
        now = datetime(2026, 10, 13, 10, 0, 37)
        got = timing.resolve_scheduled_at(publish_mode="scheduled",
                                          delay_minutes=60, now=now)
        self.assertEqual(got.second, 0)

    def test_bad_formats_raise(self):
        for bad in ("2026/10/14 08:00", "明天八点", "", "2026-13-45 08:00"):
            with self.assertRaises(ConfigError, msg=bad):
                timing.parse_datetime(bad)
        for bad in ("8点", "25:00", "08-00"):
            with self.assertRaises(ConfigError, msg=bad):
                timing.parse_hm(bad)

    def test_picker_split(self):
        self.assertEqual(timing.format_for_picker(datetime(2026, 10, 14, 8, 0)),
                         ("2026", "10", "14", "08:00"))
        self.assertEqual(timing.format_for_picker(datetime(2026, 1, 5, 23, 59)),
                         ("2026", "1", "5", "23:59"))

    def test_config_rejects_bad_mode(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"publish": {"publish_mode": "now"}})

    def test_config_rejects_scheduled_without_target(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"publish": {"publish_mode": "scheduled"}})

    def test_config_rejects_bad_scheduled_at(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"publish": {"publish_mode": "scheduled",
                                          "scheduled_at": "2026/10/14 08:00"}})

    def test_config_accepts_scheduled_with_daily_time(self):
        cfg = Config.from_dict({"publish": {"publish_mode": "scheduled",
                                            "scheduled_daily_time": "08:00"}})
        got = cfg.publish.resolve_scheduled_at(now=datetime(2026, 10, 13, 17, 50))
        self.assertEqual(got, datetime(2026, 10, 14, 8, 0))

    def test_default_config_still_draft(self):
        """默认必须是 draft —— 别让「开箱即发」这种事发生在用户没同意之前。"""
        cfg = Config.from_dict({})
        self.assertEqual(cfg.publish.publish_mode, "draft")
        self.assertIsNone(cfg.publish.resolve_scheduled_at())


class ModeOverrideTests(unittest.TestCase):
    """CLI 的 --mode / --scheduled-at 覆盖语义。"""

    def _args(self, **kw):
        import argparse
        base = {"mode": None, "scheduled_at": None}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_scheduled_at_implies_scheduled_mode(self):
        from videopub import cli

        cfg = Config.from_dict({})
        cli._apply_mode_overrides(cfg, self._args(scheduled_at="2026-10-14 08:00"))
        self.assertEqual(cfg.publish.publish_mode, "scheduled")
        self.assertEqual(cfg.publish.scheduled_at, "2026-10-14 08:00")

    def test_bad_scheduled_at_exits_with_code_2(self):
        import contextlib
        import io

        from videopub import cli

        cfg = Config.from_dict({})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                cli._apply_mode_overrides(cfg, self._args(scheduled_at="2026/10/14 08:00"))
        self.assertEqual(ctx.exception.code, 2)

    def test_scheduled_mode_without_target_exits(self):
        """--mode scheduled 但没有任何时间来源时必须报错，不能默默变成立即发布。"""
        import contextlib
        import io

        from videopub import cli

        cfg = Config.from_dict({})
        cfg.publish.scheduled_daily_time = ""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                cli._apply_mode_overrides(cfg, self._args(mode="scheduled"))
        self.assertEqual(ctx.exception.code, 2)


class ScheduleSelectorTests(unittest.TestCase):
    """「定时发表」控件选择器的防回退断言（都是实测踩过的坑）。"""

    def test_radio_uses_label_not_loose_text(self):
        """不能用 ``text=定时发表`` 定位。

        它会**子串匹配**到一段不可见的提示文案
        「使用定时发表将无法保存草稿…」，点它必然超时。
        必须落在 ``.post-time-wrap`` 里的 label 上。
        """
        from videopub.backends import channels_web as cw

        for key in ("schedule_radio_on", "schedule_radio_off"):
            self.assertIn(key, cw.SELECTORS)
            for sel in cw.SELECTORS[key]:
                self.assertNotIn("text=定时", sel, f"{key} 用了宽松文本选择器: {sel}")
                self.assertTrue(sel.startswith(".post-time-wrap"),
                                f"{key} 必须限定在 .post-time-wrap 内: {sel}")

    def test_publish_button_matches_exact_text(self):
        """``button.weui-desktop-btn_primary`` 页面上有 23 个（多为隐藏），
        按类名取首个会点错按钮 —— 必须用 :text-is() 锁死文案。
        """
        from videopub.backends import channels_web as cw

        self.assertTrue(any(":text-is('发表')" in s for s in cw.SELECTORS["publish_btn"]))
        self.assertTrue(any(":text-is('保存草稿')" in s for s in cw.SELECTORS["draft_btn"]))

    def test_schedule_flow_verifies_before_submit(self):
        """源码级护栏：设完时间必须回读校验后才允许提交。

        没有这道校验，一旦定时没设上，点「发表」就变成立即发布。
        """
        src = (ROOT / "videopub" / "backends" / "channels_web.py").read_text(encoding="utf-8")
        self.assertIn("平台定时时间设置失败", src)
        self.assertIn("_read_schedule_value", src)

    def test_apply_schedule_is_called_with_options(self):
        src = (ROOT / "videopub" / "backends" / "channels_web.py").read_text(encoding="utf-8")
        self.assertIn("self._apply_schedule(page, item, opts)", src)

    def test_time_list_selector_is_container_not_item(self):
        """时间列表存**容器**选择器，取项时拼 ``<容器> li:text-is('08')``。

        踩过的坑：存 ``ol … li`` 再用 ``_locate()`` 取 ``.first`` 会拿到单个 li，
        再往 li 里找 li 永远为空 —— 真实运行时正是死在
        ``时间列表里没有 '08'``。
        """
        from videopub.backends import channels_web as cw

        for key in ("schedule_hour_list", "schedule_minute_list"):
            self.assertIn(key, cw.SELECTORS)
            for sel in cw.SELECTORS[key]:
                self.assertTrue(sel.startswith("ol.weui-desktop-picker__time__"),
                                f"{key} 应为容器选择器: {sel}")
                self.assertNotIn(" li", sel, f"{key} 不该包含 li: {sel}")

    def test_has_locators_helper_for_enumeration(self):
        """需要枚举多个元素时必须有「未收窄」的 locator 取法。

        ``_locate()`` 返回 ``.first``，对它 ``.count()`` 恒为 1，
        曾导致日历只解析出 ``['2026年']``、月份比较失效。
        """
        from videopub.backends import channels_web as cw

        self.assertTrue(hasattr(cw.ChannelsWebBackend, "_locators"))
        self.assertTrue(hasattr(cw.ChannelsWebBackend, "_locators_any"))
        src = (ROOT / "videopub" / "backends" / "channels_web.py").read_text(encoding="utf-8")
        self.assertIn('self._locators(page, "schedule_month_label")', src)

    def test_schedule_failures_are_not_retryable(self):
        """定时设置失败是确定性失败：重试只会白白把视频再传一遍。"""
        from videopub.backends import channels_web as cw  # noqa: F401
        from videopub.errors import UploadError

        err = UploadError("x", retryable=False)
        self.assertFalse(err.retryable)
        self.assertTrue(UploadError("x").retryable)   # 类默认仍可重试


class _FakeResp:
    def __init__(self, body: str, status: int = 200) -> None:
        self._b = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    """按 URL 片段返回预设响应，用来离线验证预检逻辑。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def open(self, req, timeout=None):  # noqa: A003
        url = req.full_url
        self.calls.append(url)
        for frag, body in self.mapping.items():
            if frag in url:
                return _FakeResp(body)
        return _FakeResp("{}")


def _storage_file(tmp: Path, *, cookies=None, finder_username="v2_test@finder") -> Path:
    p = tmp / "channels_storage.json"
    p.write_text(json.dumps({
        "cookies": cookies if cookies is not None else [
            {"name": "sessionid", "value": "abc", "domain": "channels.weixin.qq.com"},
            {"name": "wxuin", "value": "12345", "domain": "channels.weixin.qq.com"},
        ],
        "origins": [{"origin": "https://channels.weixin.qq.com",
                     "localStorage": [{"name": "finder_username", "value": finder_username}]}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


class PreflightTests(unittest.TestCase):
    """发布前预检：把「平台肯不肯收视频」提前问清楚。

    背景：``helper_upload_params`` 返回非 0 时上传永远不会开始，
    页面停在 0%，提交被平台以「请上传视频」挡回。
    """

    def setUp(self):
        from videopub.config import Config

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = Config.load(None)
        self.cfg.credentials["channels_web"] = {
            "storage_state": str(_storage_file(self.tmp)),
        }

    def test_load_cookies_missing_file(self):
        from videopub import preflight
        from videopub.errors import AuthError

        with self.assertRaises(AuthError):
            preflight.load_cookies(self.tmp / "nope.json")

    def test_load_cookies_and_local_storage(self):
        from videopub import preflight

        cks = preflight.load_cookies(self.tmp / "channels_storage.json")
        self.assertEqual(cks["wxuin"], "12345")
        ls = preflight.load_local_storage(self.tmp / "channels_storage.json")
        self.assertEqual(ls["finder_username"], "v2_test@finder")

    def test_post_parses_before_truncating(self):
        """回归：必须先解析**完整**响应再截断留档。

        早期实现先 ``raw[:1500]`` 再 ``json.loads``，而 auth_data 的响应远超
        1500 字符 → JSON 被截断 → errCode 恒为 None → 预检误报「登录态不可用」。
        """
        from videopub import preflight

        big = json.dumps({"errCode": 0, "errMsg": "ok",
                          "data": {"pad": "x" * 5000}}, ensure_ascii=False)
        op = _FakeOpener({"/auth/auth_data": big})
        r = preflight._post(op, preflight.API["auth_data"], cookies={}, body={},
                            page_url="https://channels.weixin.qq.com/platform", timeout=5)
        self.assertEqual(r["errCode"], 0)
        self.assertLessEqual(len(r["raw"]), 1500)   # 留档仍要截断

    def test_ready_when_upload_params_ok(self):
        from videopub import preflight

        op = _FakeOpener({
            "/auth/auth_data": json.dumps({"errCode": 0, "data": {
                "userAttr": {"nickname": "小明"},
                "finderUser": {"nickname": "号主", "feedsCount": 3, "fansCount": 9}}}),
            "/helper/helper_upload_params": json.dumps({"errCode": 0, "data": {}}),
        })
        rep = preflight.check_upload_ready(self.cfg, opener=op)
        self.assertTrue(rep["ready"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["account"]["nickname"], "小明")
        self.assertIn("可以发布", preflight.format_report(rep))

    def test_blocked_when_upload_params_refused(self):
        """300002 必须判定为「不可发布」，且说是账号/服务端侧问题。"""
        from videopub import preflight

        op = _FakeOpener({
            "/auth/auth_data": json.dumps({"errCode": 0, "data": {
                "userAttr": {"nickname": "小明"}, "finderUser": {"feedsCount": 28}}}),
            "/helper/helper_upload_params": json.dumps(
                {"errCode": 300002, "errMsg": "request failed"}),
        })
        rep = preflight.check_upload_ready(self.cfg, opener=op)
        self.assertFalse(rep["ready"])
        self.assertEqual(rep["upload_err_code"], 300002)
        txt = preflight.format_report(rep)
        self.assertIn("暂不可发布", txt)
        self.assertIn("300002", txt)

    def test_login_failure_short_circuits(self):
        from videopub import preflight

        op = _FakeOpener({"/auth/auth_data": json.dumps({"errCode": 300002})})
        rep = preflight.check_upload_ready(self.cfg, opener=op)
        self.assertFalse(rep["ready"])
        # 登录不可用时不该再问上传参数
        self.assertFalse(any(preflight.API["upload_params"] in u for u in op.calls))
        self.assertIn("重新执行", rep["hint"])


class UploadGateTests(unittest.TestCase):
    """源码级护栏：上传必须「确认真的传上去了」才算完成。

    踩过的坑：``_upload_video`` 只要看到描述框出现就认为成功，于是
    「平台拒绝下发上传参数 → 页面永远 0%」也被判成成功，最后点了「发表」
    被平台弹「请上传视频」挡回，而结果却记成 success（假成功）。
    """

    SRC = None

    @classmethod
    def setUpClass(cls):
        cls.SRC = (ROOT / "videopub" / "backends" / "channels_web.py").read_text(encoding="utf-8")

    def test_probe_watches_helper_upload_params(self):
        from videopub.backends import channels_web as cw

        self.assertEqual(cw.UPLOAD_PARAM_HINT, "helper/helper_upload_params")
        self.assertIn("_attach_upload_probe", self.SRC)
        self.assertIn("self._attach_upload_probe(page)", self.SRC)

    def test_upload_param_failure_is_not_retryable(self):
        err = UploadError("平台拒绝下发上传参数（errCode=300002）", retryable=False)
        self.assertFalse(err.retryable)

    def test_upload_video_requires_upload_state_gone(self):
        self.assertIn("取消上传", self.SRC)
        self.assertIn("_upload_progress", self.SRC)

    def test_stall_timeout_configurable(self):
        self.assertIn("upload_stall_timeout_ms", self.SRC)

    def test_preflight_gate_before_browser_work(self):
        """发布前必须先跑上传预检，否则要白等 90s 才发现传不上去。"""
        self.assertIn("_gate_upload_ready", self.SRC)
        self.assertIn("self._gate_upload_ready()", self.SRC)
        # 预检必须排在进页面/上传之前
        self.assertLess(self.SRC.index("self._gate_upload_ready()"),
                        self.SRC.index("self._enter_create_page(page)"))

    def test_gate_only_blocks_on_conclusive_result(self):
        """预检自身出错时要放行（只告警），别把正常发布误拦掉。"""
        self.assertIn("上传预检未得出结论", self.SRC)
        self.assertIn("上传预检未完成（忽略，继续发布）", self.SRC)

    def test_upload_state_uses_locator_not_text(self):
        """上传态判定必须用 locator —— 抓文本取不到「取消上传」。"""
        from videopub.backends import channels_web as cw

        self.assertIn("upload_cancel", cw.SELECTORS)
        self.assertTrue(any("取消上传" in s for s in cw.SELECTORS["upload_cancel"]))
        self.assertIn('self._has_visible(page, "upload_cancel")', self.SRC)

    def test_upload_in_flight_must_not_fall_back_to_percent_texts(self):
        """「上传中」的判据绝不能兜底到任意 NN% 文本。

        踩过的坑：发表页常年存在 0%/100%/333% 等隐藏模板文本，
        兜底后收敛判定永不满足 → 看门狗把**已成功**的上传误判成
        「90s 进度停在 0%」（假失败）。百分比只准用于打日志。
        """
        self.assertNotIn("return bool(self._upload_progress(page))", self.SRC)
        self.assertIn("_upload_in_flight", self.SRC)
        self.assertIn("_upload_failed", self.SRC)
        # 失败态与上传中必须分开判定
        self.assertIn("upload_fail", self.SRC)
        # count()>0 不区分可见性 —— DOM 里常驻隐藏的「重新上传」模板元素，
        # 不查 is_visible 会在投递后 2 秒就误报「上传失败」
        self.assertIn("_has_visible", self.SRC)
        self.assertIn("is_visible()", self.SRC)

    def test_activity_join_before_schedule(self):
        """默认参加活动：在填描述之后、设定时之前执行，且失败不阻断发布。"""
        self.assertIn("_join_activity", self.SRC)
        self.assertIn("self._join_activity(page, activity)", self.SRC)
        idx_join = self.SRC.index("self._join_activity(page, activity)")
        idx_desc = self.SRC.index("self._fill_description(page, item)")
        idx_sched = self.SRC.index("self._apply_schedule(page, item, opts)")
        self.assertLess(idx_desc, idx_join)
        self.assertLess(idx_join, idx_sched)

    def test_activity_config_field_exists(self):
        from videopub.config import ContentConfig

        self.assertIn("activity", ContentConfig.__dataclass_fields__)
        cfg = Config.load(ROOT / "config.json")
        self.assertEqual(cfg.content.activity, "白版说知边看边学")


if __name__ == "__main__":
    unittest.main(verbosity=2)